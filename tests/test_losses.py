"""POST/GET /containers/{id}/losses 样品损耗核销端到端测试。

覆盖：
* 按核销前各祖先份额比例扣减：最大余额法分摊，各来源扣减之和严格等于总损耗，
  不产生负数，核销后来源构成与体积正确；
* 成功后容器 version 与全局 transfer_revision 各 +1（既有来源反查游标失效）；
* 边界：实测值 == 当前体积（零损耗仍写记录并递增版本/修订号）、实测值 == 0
  （整容器核销为空）、空容器（仅允许实测 0）；
* 校验失败整体回滚：实测值超过当前体积 422 measured_exceeds_volume（含
  details）、非法体积 422 invalid_volume、未知容器 404 unknown_container、
  版本不符 409 stale_container——组件、版本、修订号与损耗记录均不变；
* 只追加损耗记录：GET 按新到旧返回、含各来源扣减明细与备注；
  loss_records / loss_record_sources 由触发器禁止 UPDATE/DELETE；
* 核销不是批量转移：不产生审计记录；
* 旧库迁移：loss 表与只追加触发器在旧库上幂等补齐。
"""
import sqlite3
from decimal import Decimal

import pytest

from helpers import container, sample, volume_of

from app import service
from app.db import connect, init_db


def _mix_v1(client):
    """S1 100 / S2 100 -> V1 (40/60)。之后 V1 version=1，revision=1。"""
    sample(client, "S1", "100")
    sample(client, "S2", "100")
    container(client, "V1")
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "40"},
                {"source": "S2", "destination": "V1", "volume": "60"},
            ]
        },
    )
    assert r.status_code == 200, r.text


def _loss(client, cid, measured, expected_version, note=None):
    body = {"measured_volume": measured, "expected_version": expected_version}
    if note is not None:
        body["note"] = note
    return client.post(f"/containers/{cid}/losses", json=body)


def _revision(client, sid="S1"):
    return client.get(f"/sources/{sid}/containers").json()["transfer_revision"]


def _status(client, cid):
    r = client.get(f"/containers/{cid}")
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# 基本核销：按比例扣减、守恒、响应结构
# --------------------------------------------------------------------------- #
def test_loss_apportions_by_pre_loss_shares(client):
    _mix_v1(client)
    r = _loss(client, "V1", "75", 1, note="蒸发")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["container_id"] == "V1"
    assert body["loss_id"] == 1
    assert body["version"] == 2
    assert body["volume_before"] == "100.000000000"
    assert body["measured_volume"] == "75.000000000"
    assert body["loss_volume"] == "25.000000000"
    # 25 按核销前 40/60 比例分摊为 S1=10、S2=15
    assert body["deductions"] == [
        {"source_id": "S1", "amount": "10.000000000"},
        {"source_id": "S2", "amount": "15.000000000"},
    ]
    assert body["note"] == "蒸发"

    # 核销后容器状态：体积 75，来源 30/45（比例不变）
    status = _status(client, "V1")
    assert status["version"] == 2
    assert status["volume"] == "75.000000000"
    assert status["sources"] == [
        {"source_id": "S1", "amount": "30.000000000", "proportion": "0.400000"},
        {"source_id": "S2", "amount": "45.000000000", "proportion": "0.600000"},
    ]

    # 体积守恒：全系统各来源份额之和 = 初始体积 - 损耗
    s1 = client.get("/sources/S1/containers").json()
    assert sum(Decimal(x["source_amount"]) for x in s1["containers"]) == Decimal(
        "90.000000000"
    )
    s2 = client.get("/sources/S2/containers").json()
    assert sum(Decimal(x["source_amount"]) for x in s2["containers"]) == Decimal(
        "85.000000000"
    )


def test_loss_increments_version_and_transfer_revision(client):
    _mix_v1(client)
    assert _revision(client) == 1  # 混合批次已 +1
    assert _loss(client, "V1", "90", 1).status_code == 200
    assert _status(client, "V1")["version"] == 2
    assert _revision(client) == 2
    # 未参与核销的容器版本不变
    assert _status(client, "S1")["version"] == 1
    assert _status(client, "S2")["version"] == 1


def test_loss_on_sample_container_itself(client):
    sample(client, "S1", "10")
    r = _loss(client, "S1", "4", 0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deductions"] == [{"source_id": "S1", "amount": "6.000000000"}]
    assert _status(client, "S1")["volume"] == "4.000000000"
    items = client.get("/sources/S1/containers").json()["containers"]
    assert items[0]["source_amount"] == "4.000000000"


def test_loss_on_multi_generation_mixture(client):
    """多代混合后的容器核销：按核销前构成比例扣减，跨容器守恒保持。"""
    sample(client, "S1", "100")
    for cid in ("A", "B"):
        container(client, cid)
    client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "A", "volume": "50"},
                {"source": "A", "destination": "B", "volume": "20"},
            ]
        },
    )
    # B 含 S1=20；核销到 5 -> 损耗 15 全部来自 S1
    r = _loss(client, "B", "5", 1)
    assert r.status_code == 200, r.text
    assert r.json()["deductions"] == [
        {"source_id": "S1", "amount": "15.000000000"}
    ]
    items = client.get("/sources/S1/containers").json()["containers"]
    total = sum(Decimal(x["source_amount"]) for x in items)
    assert total == Decimal("85.000000000")


# --------------------------------------------------------------------------- #
# 边界：零损耗 / 全额核销 / 空容器
# --------------------------------------------------------------------------- #
def test_zero_loss_when_measured_equals_current(client):
    _mix_v1(client)
    r = _loss(client, "V1", "100", 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["loss_volume"] == "0.000000000"
    assert body["deductions"] == []
    # 零损耗同样递增版本与修订号，并写入损耗记录
    assert _status(client, "V1")["version"] == 2
    assert _revision(client) == 2
    losses = client.get("/containers/V1/losses").json()
    assert losses["count"] == 1
    assert losses["losses"][0]["loss_volume"] == "0.000000000"
    assert losses["losses"][0]["deductions"] == []
    # 来源构成完全不变
    assert _status(client, "V1")["sources"] == [
        {"source_id": "S1", "amount": "40.000000000", "proportion": "0.400000"},
        {"source_id": "S2", "amount": "60.000000000", "proportion": "0.600000"},
    ]


def test_full_writeoff_with_measured_zero(client):
    _mix_v1(client)
    r = _loss(client, "V1", "0", 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["loss_volume"] == "100.000000000"
    assert body["deductions"] == [
        {"source_id": "S1", "amount": "40.000000000"},
        {"source_id": "S2", "amount": "60.000000000"},
    ]
    status = _status(client, "V1")
    assert status["volume"] == "0"
    assert status["sources"] == []
    # V1 不再持有任何来源份额，从反向追踪结果中消失
    ids = [x["container_id"] for x in client.get("/sources/S1/containers").json()["containers"]]
    assert "V1" not in ids


def test_empty_container_only_allows_zero_measured(client):
    container(client, "E1")
    # 空容器当前体积为 0：实测 1 超过当前体积
    r = _loss(client, "E1", "1", 0)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "measured_exceeds_volume"
    # 实测 0：零损耗核销成功
    r = _loss(client, "E1", "0", 0)
    assert r.status_code == 200, r.text
    assert r.json()["loss_volume"] == "0.000000000"
    assert _status(client, "E1")["version"] == 1


# --------------------------------------------------------------------------- #
# 最大余额法：扣减之和严格等于总损耗、无负数
# --------------------------------------------------------------------------- #
def test_apportion_uses_largest_remainder_and_conserves_exactly(client):
    sample(client, "S1", "1")
    sample(client, "S2", "1")
    sample(client, "S3", "1")
    container(client, "V1")
    client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "1"},
                {"source": "S2", "destination": "V1", "volume": "1"},
                {"source": "S3", "destination": "V1", "volume": "1"},
            ]
        },
    )
    # V1 各来源均为 1；核销 1（实测 2）：1/3 除不尽，最大余额法补 1 个刻度
    r = _loss(client, "V1", "2", 1)
    assert r.status_code == 200, r.text
    deductions = {d["source_id"]: Decimal(d["amount"]) for d in r.json()["deductions"]}
    # 与 service.apportion 的确定性结果一致：平局按来源 ID 打破，S1 多得 1 刻度
    assert deductions == {
        "S1": Decimal("0.333333334"),
        "S2": Decimal("0.333333333"),
        "S3": Decimal("0.333333333"),
    }
    # 各来源扣减之和严格等于总损耗
    assert sum(deductions.values(), Decimal(0)) == Decimal("1.000000000")
    # 核销后各来源份额非负，且构成之和精确等于实测体积
    status = _status(client, "V1")
    after = {s["source_id"]: Decimal(s["amount"]) for s in status["sources"]}
    assert all(amount >= 0 for amount in after.values())
    assert sum(after.values(), Decimal(0)) == Decimal("2.000000000")


def test_repeated_losses_never_go_negative_and_stay_conserved(client):
    sample(client, "S1", "10")
    container(client, "V1")
    client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "7"}]},
    )
    # 逐次核销到 5 -> 3 -> 2 -> 0，每次检查版本递增与构成非负
    for measured, expected_version in (("5", 1), ("3", 2), ("2", 3), ("0", 4)):
        r = _loss(client, "V1", measured, expected_version)
        assert r.status_code == 200, r.text
        deductions = r.json()["deductions"]
        assert sum(Decimal(d["amount"]) for d in deductions) == Decimal(
            r.json()["loss_volume"]
        )
        status = _status(client, "V1")
        assert Decimal(status["volume"]) == Decimal(measured)
        assert all(Decimal(s["amount"]) >= 0 for s in status["sources"])
    # 反向追踪：S1 总份额 = 初始 10 - 累计损耗 7
    items = client.get("/sources/S1/containers").json()["containers"]
    assert sum(Decimal(x["source_amount"]) for x in items) == Decimal("3.000000000")


# --------------------------------------------------------------------------- #
# 校验失败：整体回滚，组件 / 版本 / 修订号 / 记录均不变
# --------------------------------------------------------------------------- #
def test_measured_above_current_422_and_nothing_changes(client):
    _mix_v1(client)
    r = _loss(client, "V1", "100.000000001", 1)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "measured_exceeds_volume"
    assert err["details"] == {
        "container_id": "V1",
        "measured_volume": "100.000000001",
        "current_volume": "100.000000000",
    }
    # 组件、版本、修订号与损耗记录均不变
    assert _status(client, "V1")["version"] == 1
    assert volume_of(client, "V1") == "100.000000000"
    assert _revision(client) == 1
    assert client.get("/containers/V1/losses").json()["count"] == 0


def test_invalid_measured_values_422_and_nothing_changes(client):
    _mix_v1(client)
    # 负数 / 超出 DECIMAL_SCALE 精度：业务层 422 invalid_volume（错误信封）
    for bad in ("-1", "1.0000000001"):
        r = _loss(client, "V1", bad, 1)
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["error"]["code"] == "invalid_volume", bad
    # 非有限数：schema 层同样拒绝为 422（与其它接口的体积字段一致）
    for bad in ("NaN", "Infinity"):
        assert _loss(client, "V1", bad, 1).status_code == 422, bad
    assert _status(client, "V1")["version"] == 1
    assert _revision(client) == 1
    assert client.get("/containers/V1/losses").json()["count"] == 0


def test_unknown_container_404(client):
    r = _loss(client, "GHOST", "1", 0)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_container"


def test_stale_version_409_and_nothing_changes(client):
    _mix_v1(client)
    r = _loss(client, "V1", "75", 0)  # 实际版本已是 1
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "stale_container"
    assert _status(client, "V1")["version"] == 1
    assert volume_of(client, "V1") == "100.000000000"
    assert _revision(client) == 1
    assert client.get("/containers/V1/losses").json()["count"] == 0


def test_expected_version_must_track_each_successful_loss(client):
    _mix_v1(client)
    assert _loss(client, "V1", "90", 1).status_code == 200
    # 旧版本立即失效
    assert _loss(client, "V1", "80", 1).status_code == 409
    # 用新版本继续核销成功
    assert _loss(client, "V1", "80", 2).status_code == 200
    assert _status(client, "V1")["version"] == 3


def test_schema_level_validation_errors(client):
    _mix_v1(client)
    # 缺少 expected_version / measured_volume
    assert client.post("/containers/V1/losses", json={"measured_volume": "1"}).status_code == 422
    assert client.post("/containers/V1/losses", json={"expected_version": 1}).status_code == 422
    # 布尔 / 负数版本
    assert _loss(client, "V1", "1", True).status_code == 422
    assert _loss(client, "V1", "1", -1).status_code == 422
    # 任何失败都不留痕
    assert client.get("/containers/V1/losses").json()["count"] == 0
    assert _status(client, "V1")["version"] == 1


# --------------------------------------------------------------------------- #
# 只追加损耗记录：GET 按新到旧；触发器禁止 UPDATE/DELETE
# --------------------------------------------------------------------------- #
def test_get_losses_returns_newest_first_with_details(client):
    _mix_v1(client)
    _loss(client, "V1", "90", 1, note="第一次")
    _loss(client, "V1", "80", 2)
    _loss(client, "V1", "70", 3, note="第三次")
    r = client.get("/containers/V1/losses")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["container_id"] == "V1"
    assert body["count"] == 3
    # 新到旧：loss_id 倒序
    assert [x["loss_id"] for x in body["losses"]] == [3, 2, 1]
    newest = body["losses"][0]
    assert newest["measured_volume"] == "70.000000000"
    assert newest["loss_volume"] == "10.000000000"
    assert newest["note"] == "第三次"
    assert newest["created"] is not None
    # 每条记录的扣减明细之和严格等于其总损耗
    for rec in body["losses"]:
        assert sum(Decimal(d["amount"]) for d in rec["deductions"]) == Decimal(
            rec["loss_volume"]
        )
    # 未提供备注的记录 note 为 null
    assert body["losses"][1]["note"] is None


def test_get_losses_empty_for_fresh_container_and_404_for_unknown(client):
    container(client, "V1")
    body = client.get("/containers/V1/losses").json()
    assert body == {"container_id": "V1", "count": 0, "losses": []}
    r = client.get("/containers/GHOST/losses")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_container"


def test_loss_records_are_append_only_via_triggers(client):
    _mix_v1(client)
    _loss(client, "V1", "90", 1)
    conn = connect()
    try:
        with pytest.raises(sqlite3.DatabaseError) as exc:
            conn.execute("UPDATE loss_records SET note = 'x' WHERE id = 1")
        assert "只追加" in str(exc.value)
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM loss_records WHERE id = 1")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE loss_record_sources SET amount = '0' WHERE loss_id = 1")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM loss_record_sources WHERE loss_id = 1")
        # 记录本身完好
        row = conn.execute("SELECT note FROM loss_records WHERE id = 1").fetchone()
        assert row["note"] is None
    finally:
        conn.close()


def test_loss_does_not_write_audit_record(client):
    _mix_v1(client)
    assert client.get("/audit/records").json()["count"] == 1  # 混合批次
    _loss(client, "V1", "90", 1)
    assert client.get("/audit/records").json()["count"] == 1


def test_loss_invalidates_source_reverse_lookup_cursors(client):
    _mix_v1(client)
    token = client.get("/sources/S1/containers", params={"limit": 1}).json()[
        "next_cursor"
    ]
    assert token is not None
    _loss(client, "V1", "90", 1)
    r = client.get("/sources/S1/containers", params={"limit": 1, "cursor": token})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_cursor"
    assert r.json()["error"]["details"] == {
        "cursor_revision": 1,
        "current_revision": 2,
    }


def test_failed_loss_does_not_invalidate_cursors(client):
    _mix_v1(client)
    token = client.get("/sources/S1/containers", params={"limit": 1}).json()[
        "next_cursor"
    ]
    # 版本冲突与校验失败都不递增修订号
    assert _loss(client, "V1", "90", 0).status_code == 409
    assert _loss(client, "V1", "999", 1).status_code == 422
    r = client.get("/sources/S1/containers", params={"limit": 1, "cursor": token})
    assert r.status_code == 200


def test_decimal_fields_serialize_as_strings(client):
    _mix_v1(client)
    _loss(client, "V1", "75", 1)
    import json

    post_body = json.loads(client.get("/containers/V1/losses").text)
    for rec in post_body["losses"]:
        assert isinstance(rec["measured_volume"], str)
        assert isinstance(rec["loss_volume"], str)
        for d in rec["deductions"]:
            assert isinstance(d["amount"], str)


# --------------------------------------------------------------------------- #
# 旧库迁移：loss 表与只追加触发器幂等补齐
# --------------------------------------------------------------------------- #
def test_init_db_migrates_legacy_db_with_loss_tables(tmp_path, monkeypatch):
    legacy_path = tmp_path / "legacy_loss.db"
    raw = sqlite3.connect(legacy_path)
    try:
        raw.executescript(
            """
            CREATE TABLE containers (
                id TEXT PRIMARY KEY,
                created INTEGER NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE components (
                container_id TEXT NOT NULL REFERENCES containers(id) ON DELETE CASCADE,
                source_id    TEXT NOT NULL,
                amount       TEXT NOT NULL,
                PRIMARY KEY (container_id, source_id)
            );
            INSERT INTO containers (id) VALUES ('OLD');
            INSERT INTO components (container_id, source_id, amount)
                VALUES ('OLD', 'OLD', '5');
            """
        )
        raw.commit()
    finally:
        raw.close()

    monkeypatch.setattr("app.config.settings.database_path", str(legacy_path))
    init_db()
    init_db()  # 幂等

    conn = connect()
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "loss_records" in tables
        assert "loss_record_sources" in tables
        triggers = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert {
            "loss_records_no_update",
            "loss_records_no_delete",
            "loss_record_sources_no_update",
            "loss_record_sources_no_delete",
        } <= triggers

        # 迁移后的旧库可直接核销：版本、修订号与记录正常工作
        out = service.record_container_loss(conn, "OLD", Decimal("3"), 0, "迁移后核销")
        assert out["loss_volume"] == "2.000000000"
        assert out["version"] == 1
        from app import loss as loss_module

        listed = loss_module.list_loss_records(conn, "OLD")
        assert listed["count"] == 1
        assert listed["losses"][0]["note"] == "迁移后核销"
        assert listed["losses"][0]["deductions"] == [
            {"source_id": "OLD", "amount": "2.000000000"}
        ]
        rev = conn.execute(
            "SELECT revision FROM transfer_revision WHERE id = 1"
        ).fetchone()[0]
        assert rev == 1
    finally:
        conn.close()
