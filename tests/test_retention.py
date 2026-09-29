"""PUT/GET /containers/{id}/retention 最低留存份额锁定端到端测试。

覆盖：
* 设置 / 修改 / 解除留存规则；返回当前全部规则、各来源当前份额与版本；
* 来源必须已登记为初始样品（422 unknown_sample）；容器不存在 404；
* 正值必须 > 0（0 / 负数 / 超精度 422 invalid_volume），且不得超过该来源
  当前份额（422 retention_exceeds_share）；
* expected_version 过期 409 stale_container；非法 / 过期时规则与版本均不变；
* 每次实际变更 version +1，幂等重写同值 / 解除不存在的规则版本不变；
  留存变更不递增 transfer_revision、不产生审计记录；
* 批量提交与预演在每一步定点分摊后检查源容器各留存份额：任一步低于下限
  整批拒绝（422 retention_breach，含 index/source/source_id/remaining/
  min_volume details），即使同批后续会转回也一样；恰好等于下限允许；
  转入不受约束；失败时份额、版本、修订号、审计、幂等均不变；
* 损耗核销按实际来源扣减执行同一约束；失败时组件 / 版本 / 修订号 / 记录不变；
* 旧库启动幂等补出 retention_locks 表。
"""
from decimal import Decimal

from helpers import container, sample

from app.db import connect, init_db


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _put_retention(client, cid, source_id, min_volume, expected_version):
    return client.put(
        f"/containers/{cid}/retention",
        json={
            "source_id": source_id,
            "min_volume": min_volume,
            "expected_version": expected_version,
        },
    )


def _get_retention(client, cid):
    return client.get(f"/containers/{cid}/retention")


def _status(client, cid):
    r = client.get(f"/containers/{cid}")
    assert r.status_code == 200, r.text
    return r.json()


def _revision(client, sid="S1"):
    return client.get(f"/sources/{sid}/containers").json()["transfer_revision"]


def _mix(client):
    """S1 100 / S2 100 -> V1(40/60)。V1 version=1, revision=1。"""
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


# --------------------------------------------------------------------------- #
# 设置 / 查询 / 修改 / 解除
# --------------------------------------------------------------------------- #
def test_set_get_and_release_retention_lock(client):
    _mix(client)

    # V1 当前 S1=40；锁定最低留存 30
    r = _put_retention(client, "V1", "S1", "30", 1)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "container_id": "V1",
        "version": 2,
        "retention": [
            {
                "source_id": "S1",
                "min_volume": "30.000000000",
                "current_share": "40.000000000",
            }
        ],
    }

    # GET 返回相同视图
    r = _get_retention(client, "V1")
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    assert len(r.json()["retention"]) == 1

    # 再锁 S2=60（多条规则按 source_id 排序）
    r = _put_retention(client, "V1", "S2", "60", 2)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 3
    assert [x["source_id"] for x in body["retention"]] == ["S1", "S2"]

    # 解除 S1（null）：规则减少，version +1
    r = _put_retention(client, "V1", "S1", None, 3)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 4
    assert [x["source_id"] for x in body["retention"]] == ["S2"]

    # 解除后版本也体现在 GET /containers
    assert _status(client, "V1")["version"] == 4


def test_lock_can_be_set_on_sample_container_itself(client):
    sample(client, "S1", "100")
    r = _put_retention(client, "S1", "S1", "80", 0)
    assert r.status_code == 200, r.text
    assert r.json()["retention"][0]["current_share"] == "100.000000000"


def test_empty_rules_for_fresh_container_and_404(client):
    _mix(client)
    assert _get_retention(client, "V2" if False else "S1").status_code == 200
    r = client.get("/containers/GHOST/retention")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_container"
    r = _put_retention(client, "GHOST", "S1", "1", 0)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_container"


# --------------------------------------------------------------------------- #
# 实际变更才 +1：幂等重写 / 解除不存在的规则
# --------------------------------------------------------------------------- #
def test_actual_change_bumps_version_noop_does_not(client):
    _mix(client)
    assert _put_retention(client, "V1", "S1", "30", 1).json()["version"] == 2

    # 同值重写：不是实际变更，版本不变；用最新 expected_version=2
    r = _put_retention(client, "V1", "S1", "30", 2)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2

    # 改成不同值：+1
    assert _put_retention(client, "V1", "S1", "35", 2).json()["version"] == 3

    # 解除一条不存在的规则（S2 未锁定）：版本不变
    r = _put_retention(client, "V1", "S2", None, 3)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 3
    assert [x["source_id"] for x in r.json()["retention"]] == ["S1"]

    # 字符串 "30.0" 与 "30.000000000" 数值相同：仍非实际变更
    r = _put_retention(client, "V1", "S1", "35.000000000", 3)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 3


def test_retention_change_does_not_touch_revision_or_audit(client):
    _mix(client)
    assert _revision(client) == 1
    _put_retention(client, "V1", "S1", "30", 1)
    _put_retention(client, "V1", "S1", "20", 2)
    _put_retention(client, "V1", "S1", None, 3)
    # transfer_revision 不随留存变更变化
    assert _revision(client) == 1
    # 留存变更不是批次：审计仍只有混合批次 1 条
    assert client.get("/audit/records").json()["count"] == 1


# --------------------------------------------------------------------------- #
# 参数 / 状态校验
# --------------------------------------------------------------------------- #
def test_stale_expected_version_409_and_nothing_changes(client):
    _mix(client)
    r = _put_retention(client, "V1", "S1", "30", 0)  # V1 当前 version=1
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "stale_container"
    assert _get_retention(client, "V1").json()["retention"] == []
    assert _status(client, "V1")["version"] == 1
    assert _revision(client) == 1


def test_source_must_be_registered_sample(client):
    _mix(client)
    container(client, "E1")
    # 未知 ID
    r = _put_retention(client, "V1", "NOPE", "1", 1)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "unknown_sample"
    assert err["details"]["unknown_samples"] == ["NOPE"]
    # 已登记但只是空容器
    r = _put_retention(client, "V1", "E1", "1", 1)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unknown_sample"
    # 失败不留痕
    assert _get_retention(client, "V1").json()["retention"] == []
    assert _status(client, "V1")["version"] == 1


def test_min_volume_validation(client):
    _mix(client)
    # 0 -> invalid_volume（解除应使用 null）
    for bad in ("0", "-1", "1.0000000001"):
        r = _put_retention(client, "V1", "S1", bad, 1)
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["error"]["code"] == "invalid_volume", bad
    # 非有限数：schema 层 422
    for bad in ("NaN", "Infinity"):
        assert _put_retention(client, "V1", "S1", bad, 1).status_code == 422
    assert _get_retention(client, "V1").json()["retention"] == []
    assert _status(client, "V1")["version"] == 1


def test_missing_required_fields_422(client):
    _mix(client)
    assert client.put(
        "/containers/V1/retention", json={"source_id": "S1", "min_volume": "1"}
    ).status_code == 422
    assert client.put(
        "/containers/V1/retention",
        json={"min_volume": "1", "expected_version": 1},
    ).status_code == 422
    # 布尔 / 负数版本
    assert _put_retention(client, "V1", "S1", "1", True).status_code == 422
    assert _put_retention(client, "V1", "S1", "1", -1).status_code == 422


def test_min_volume_must_not_exceed_current_share(client):
    _mix(client)
    # V1 中 S1=40：锁 41 超过当前份额
    r = _put_retention(client, "V1", "S1", "41", 1)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "retention_exceeds_share"
    assert err["details"] == {
        "container_id": "V1",
        "source_id": "S1",
        "min_volume": "41.000000000",
        "current_share": "40.000000000",
    }
    # 容器中没有该来源份额（S3 是样品但 V1 不含 S3）：份额 0，任何正值都超过
    sample(client, "S3", "10")
    r = _put_retention(client, "V1", "S3", "0.000000001", 1)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "retention_exceeds_share"
    assert r.json()["error"]["details"]["current_share"] == "0.000000000"
    # 恰好等于当前份额：允许
    r = _put_retention(client, "V1", "S1", "40", 1)
    assert r.status_code == 200, r.text
    # 失败 / 成功后版本如预期
    assert _status(client, "V1")["version"] == 2


# --------------------------------------------------------------------------- #
# 批量转移：分摊后检查源容器留存
# --------------------------------------------------------------------------- #
def test_batch_rejects_transfer_dropping_source_below_lock(client):
    """V1(S1=40/S2=60) 锁 S1 最低 30；转出 26 使 S1 剩 29.6，整批拒绝。"""
    _mix(client)
    assert _put_retention(client, "V1", "S1", "30", 1).status_code == 200
    container(client, "V2")

    # 转出 26：按 40/60 分摊 S1 扣 10.4 -> 剩 29.6 < 30
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "V1", "destination": "V2", "volume": "26"}]},
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "retention_breach"
    assert err["details"] == {
        "index": 1,
        "source": "V1",
        "container_id": "V1",
        "source_id": "S1",
        "remaining": "29.600000000",
        "min_volume": "30.000000000",
    }
    # 拒绝后份额 / 版本 / 修订号均不变
    assert _revision(client) == 1
    assert _status(client, "V1")["version"] == 2
    assert Decimal(_status(client, "V1")["volume"]) == Decimal("100.000000000")


def test_batch_lock_boundaries_equal_allowed_below_rejected(client):
    _mix(client)
    assert _put_retention(client, "V1", "S1", "30", 1).status_code == 200
    container(client, "V2")

    # 转出 25：S1 扣 10、S2 扣 15 -> S1 剩恰好 30（等于下限），允许
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "V1", "destination": "V2", "volume": "25"}]},
    )
    assert r.status_code == 200, r.text
    status = _status(client, "V1")
    assert status["version"] == 3  # 留存变更 +1 后批次再 +1
    shares = {s["source_id"]: Decimal(s["amount"]) for s in status["sources"]}
    assert shares["S1"] == Decimal("30.000000000")

    # 再转出 1（此时 V1=75，S1=30）：S1 被扣 0.4 -> 剩 29.6 < 30，整批拒绝
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "V1", "destination": "V2", "volume": "1"}]},
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "retention_breach"
    assert err["details"]["index"] == 1
    assert err["details"]["source"] == "V1"
    assert err["details"]["container_id"] == "V1"
    assert err["details"]["source_id"] == "S1"
    assert Decimal(err["details"]["remaining"]) == Decimal("29.600000000")
    assert Decimal(err["details"]["min_volume"]) == Decimal("30.000000000")
    # 拒绝后份额、版本、修订号均不变
    status = _status(client, "V1")
    assert status["version"] == 3
    assert Decimal(status["volume"]) == Decimal("75.000000000")
    assert _revision(client) == 2  # mix=1, 转出 25 批次=2；被拒批次不递增


def test_batch_checked_after_apportionment_each_step_even_if_returned_later(client):
    """同批先跌破、后转回：第一步已违规即整批拒绝（即使最终份额满足）。"""
    _mix(client)  # S1=100, S2=100, V1(S1=40,S2=60), V1 version=1
    container(client, "V2")
    assert _put_retention(client, "V1", "S1", "35", 1).status_code == 200

    # 步骤1: V1 -> V2 取 20（S1 扣 8 剩 32 < 35）→ 立即拒绝；
    # 步骤2 即使把 S1 转回 V1 也不予放行
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "V1", "destination": "V2", "volume": "20"},
                {"source": "S1", "destination": "V1", "volume": "8"},
            ]
        },
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "retention_breach"
    assert err["details"]["index"] == 1
    # 整批不落库
    status = _status(client, "V1")
    assert status["version"] == 2
    shares = {s["source_id"]: s["amount"] for s in status["sources"]}
    assert shares == {"S1": "40.000000000", "S2": "60.000000000"}
    assert _revision(client) == 1
    # V2 仍为空
    assert _status(client, "V2")["volume"] == "0"


def test_later_step_in_batch_violation_reports_that_step(client):
    """同批前面的转出未违规、后面的转出跌破：报后续步骤的序号。"""
    _mix(client)
    container(client, "V2")
    assert _put_retention(client, "V1", "S1", "30", 1).status_code == 200
    # 第 1 步取 20：S1 剩 32（合规）；第 2 步再取 10：S1 剩 28 < 30
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "V1", "destination": "V2", "volume": "20"},
                {"source": "V1", "destination": "V2", "volume": "10"},
            ]
        },
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "retention_breach"
    assert err["details"]["index"] == 2
    assert Decimal(_status(client, "V1")["volume"]) == Decimal("100.000000000")


def test_incoming_transfer_does_not_check_destination_locks(client):
    """转入只增加份额：目标容器的留存规则不参与检查。"""
    _mix(client)
    # V2 为空，不能对份额 0 设锁；先向 V2 转 5，再锁
    container(client, "V2")
    assert client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V2", "volume": "5"}]},
    ).status_code == 200
    assert _put_retention(client, "V2", "S1", "5", 1).status_code == 200
    # 继续向 V2 转入：不触发留存校验（容量 / 不相容之外无约束）
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V2", "volume": "3"}]},
    )
    assert r.status_code == 200, r.text
    assert Decimal(_status(client, "V2")["volume"]) == Decimal("8.000000000")


def test_preview_enforces_retention_without_writing(client):
    _mix(client)
    container(client, "V2")
    assert _put_retention(client, "V1", "S1", "30", 1).status_code == 200

    # 预演违规批次：与提交一致的 422 / details
    r = client.post(
        "/transfers/preview",
        json={"transfers": [{"source": "V1", "destination": "V2", "volume": "26"}]},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "retention_breach"

    # 预演合规批次成功返回，且数据库不变（版本不递增）
    r = client.post(
        "/transfers/preview",
        json={"transfers": [{"source": "V1", "destination": "V2", "volume": "25"}]},
    )
    assert r.status_code == 200, r.text
    assert _status(client, "V1")["version"] == 2  # 仅留存变更的 +1
    assert Decimal(_status(client, "V1")["volume"]) == Decimal("100.000000000")
    assert _revision(client) == 1


def test_preview_expected_versions_includes_retention_bump(client):
    _mix(client)
    container(client, "V2")
    assert _put_retention(client, "V1", "S1", "30", 1).status_code == 200
    # V1 版本已为 2：预演带过期 expected_versions 仍 409
    r = client.post(
        "/transfers/preview",
        json={
            "transfers": [{"source": "V1", "destination": "V2", "volume": "25"}],
            "expected_versions": {"V1": 1, "V2": 0},
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_container"
    # 正确版本可预演
    r = client.post(
        "/transfers/preview",
        json={
            "transfers": [{"source": "V1", "destination": "V2", "volume": "25"}],
            "expected_versions": {"V1": 2, "V2": 0},
        },
    )
    assert r.status_code == 200, r.text


def test_rejected_batch_writes_no_audit_or_idempotency_record(client):
    _mix(client)
    container(client, "V2")
    assert _put_retention(client, "V1", "S1", "30", 1).status_code == 200
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [{"source": "V1", "destination": "V2", "volume": "26"}],
            "idempotency_key": "key-retention-1",
        },
    )
    assert r.status_code == 422
    assert client.get("/audit/records").json()["count"] == 1  # 仅混合批次
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0
    finally:
        conn.close()
    # 失败后同键可正常用于另一请求
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [{"source": "V1", "destination": "V2", "volume": "25"}],
            "idempotency_key": "key-retention-1",
        },
    )
    assert r.status_code == 200, r.text


def test_lock_release_allows_transfer_again(client):
    _mix(client)
    container(client, "V2")
    assert _put_retention(client, "V1", "S1", "30", 1).status_code == 200
    body = {"transfers": [{"source": "V1", "destination": "V2", "volume": "26"}]}
    assert client.post("/transfers/batch", json=body).status_code == 422
    # 解除（V1 version=2）
    assert _put_retention(client, "V1", "S1", None, 2).status_code == 200
    assert client.post("/transfers/batch", json=body).status_code == 200
    assert Decimal(_status(client, "V1")["volume"]) == Decimal("74.000000000")


def test_lock_on_sample_container_blocks_outflow(client):
    sample(client, "S1", "100")
    container(client, "V1")
    assert _put_retention(client, "S1", "S1", "80", 0).status_code == 200
    # 转出 21：S1 剩 79 < 80
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "21"}]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "retention_breach"
    assert Decimal(_status(client, "S1")["volume"]) == Decimal("100.000000000")
    # 转出 20：恰好剩 80，允许
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "20"}]},
    )
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# 损耗核销：按实际来源扣减后执行同一约束
# --------------------------------------------------------------------------- #
def test_loss_rejected_when_deduction_breaches_retention(client):
    _mix(client)  # V1(S1=40,S2=60) version=1
    assert _put_retention(client, "V1", "S1", "35", 1).status_code == 200

    # 核销到 75（损耗 25）：S1 扣 10 剩 30 < 35 -> 拒绝
    r = client.post(
        "/containers/V1/losses",
        json={"measured_volume": "75", "expected_version": 2},
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "retention_breach"
    assert "index" not in err["details"]
    assert err["details"]["container_id"] == "V1"
    assert err["details"]["source_id"] == "S1"
    assert Decimal(err["details"]["remaining"]) == Decimal("30.000000000")
    assert Decimal(err["details"]["min_volume"]) == Decimal("35.000000000")

    # 组件 / 版本 / 修订号 / 损耗记录均不变
    status = _status(client, "V1")
    assert status["version"] == 2
    assert Decimal(status["volume"]) == Decimal("100.000000000")
    assert _revision(client) == 1
    assert client.get("/containers/V1/losses").json()["count"] == 0


def test_loss_allowed_when_share_stays_at_or_above_lock(client):
    _mix(client)
    assert _put_retention(client, "V1", "S1", "30", 1).status_code == 200
    # 核销到 75：S1 剩恰好 30，允许
    r = client.post(
        "/containers/V1/losses",
        json={"measured_volume": "75", "expected_version": 2},
    )
    assert r.status_code == 200, r.text
    status = _status(client, "V1")
    assert status["version"] == 3
    shares = {s["source_id"]: Decimal(s["amount"]) for s in status["sources"]}
    assert shares["S1"] == Decimal("30.000000000")


def test_zero_loss_always_passes_retention(client):
    _mix(client)
    assert _put_retention(client, "V1", "S1", "40", 1).status_code == 200
    r = client.post(
        "/containers/V1/losses",
        json={"measured_volume": "100", "expected_version": 2},
    )
    assert r.status_code == 200, r.text
    assert r.json()["loss_volume"] == "0.000000000"


# --------------------------------------------------------------------------- #
# 旧库迁移：retention_locks 幂等补齐
# --------------------------------------------------------------------------- #
def test_init_db_migrates_legacy_db_with_retention_table(tmp_path, monkeypatch):
    import sqlite3

    legacy_path = tmp_path / "legacy_retention.db"
    raw = sqlite3.connect(legacy_path)
    try:
        raw.executescript(
            """
            CREATE TABLE containers (
                id TEXT PRIMARY KEY,
                created INTEGER NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE components (
                container_id TEXT NOT NULL REFERENCES containers(id),
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
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "retention_locks" in tables
        # 迁移后的旧库可直接登记初始样品并设置留存（OLD 经迁移标记为样品）
        from decimal import Decimal as D

        out = service_set_retention(conn, "OLD", "OLD", D("4"), 0)
        assert out["version"] == 1
        assert out["retention"][0]["min_volume"] == "4.000000000"
        # 旧库份额按原样字符串 '5' 存储（与 GET /containers 的展示一致）
        assert out["retention"][0]["current_share"] == "5"
    finally:
        conn.close()


def service_set_retention(conn, cid, sid, min_volume, expected_version):
    from app import service

    return service.set_container_retention(conn, cid, sid, min_volume, expected_version)
