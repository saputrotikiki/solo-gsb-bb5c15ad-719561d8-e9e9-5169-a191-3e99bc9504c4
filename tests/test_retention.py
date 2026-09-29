"""最低留存份额（retention_rules）端到端测试。

覆盖：
* PUT /containers/{id}/retention/{source_id} 锁定 / 修改 / 解除最低留存份额：
  来源须已登记为初始样品（422 unknown_sample），正值不得超过该来源当前份额
  （422 retention_above_share，含 details），0 / 负 / 超精度 422 invalid_volume，
  容器不存在 404 unknown_container，版本不符 409 stale_container；
* 只有**实际变更**使容器 version +1（设为相同值、解除不存在的规则为无变更），
  且不改变全局 transfer_revision、不写审计记录、不使来源反查游标失效；
* GET /containers/{id}/retention 返回当前规则（含各来源当前份额、按 source_id
  排序），未知容器 404，空规则返回空列表；
* 批量提交与预演在顺序模拟的**每一步定点分摊后**检查**源容器**全部留存份额，
  任一步低于下限即整批拒绝（422 retention_below_minimum，details 含条目序号、
  源容器与全部违约来源的剩余 / 下限 / 差额）——即使同批后续步骤会转回也一样；
  恰好等于下限允许；只作为目标的容器不受留存约束；
* 拒绝时份额、各容器 version、transfer_revision、幂等记录与审计链均不变；
  预演成功 / 失败都不落库；失败请求的幂等键可复用，已成功请求的重放不重新校验；
* 损耗核销按实际来源扣减后执行同一约束：违约整体回滚（version / 修订号 /
  损耗记录均不变），恰好扣到下限允许，零损耗允许；
* 旧库启动时幂等创建 retention_rules 表。
"""
from decimal import Decimal

from helpers import container, sample, volume_of

from app import service
from app.db import connect, init_db


def _put_retention(client, cid, sid, minimum, expected_version):
    return client.put(
        f"/containers/{cid}/retention/{sid}",
        json={"minimum": minimum, "expected_version": expected_version},
    )


def _get_retention(client, cid):
    return client.get(f"/containers/{cid}/retention")


def _rules(client, cid):
    body = _get_retention(client, cid).json()
    return {r["source_id"]: r for r in body["rules"]}


def _status(client, cid):
    r = client.get(f"/containers/{cid}")
    assert r.status_code == 200, r.text
    return r.json()


def _revision(client, sid="S1"):
    return client.get(f"/sources/{sid}/containers").json()["transfer_revision"]


def _mix_v1(client):
    """S1 100 / S2 100 -> V1 (40/60)。之后 V1 / S1 / S2 version=1，revision=1。"""
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
# 设置 / 修改 / 查询 / 解除
# --------------------------------------------------------------------------- #
def test_put_retention_locks_and_get_returns_current_amount(client):
    _mix_v1(client)
    r = _put_retention(client, "V1", "S1", "30", 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["container_id"] == "V1"
    assert body["source_id"] == "S1"
    assert body["changed"] is True
    assert body["version"] == 2
    assert body["minimum"] == "30.000000000"
    assert body["rules"] == [
        {
            "source_id": "S1",
            "minimum": "30.000000000",
            "current_amount": "40.000000000",
        }
    ]

    got = _get_retention(client, "V1")
    assert got.status_code == 200, got.text
    assert got.json() == {
        "container_id": "V1",
        "version": 2,
        "rules": [
            {
                "source_id": "S1",
                "minimum": "30.000000000",
                "current_amount": "40.000000000",
            }
        ],
    }


def test_put_retention_on_sample_container_itself(client):
    sample(client, "S1", "100")
    r = _put_retention(client, "S1", "S1", "80", 0)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1
    assert _rules(client, "S1")["S1"]["current_amount"] == "100.000000000"


def test_update_existing_rule_bumps_version_only_when_value_changes(client):
    _mix_v1(client)
    assert _put_retention(client, "V1", "S1", "30", 1).json()["version"] == 2
    # 修改为不同值：version 再 +1
    r = _put_retention(client, "V1", "S1", "35", 2)
    assert r.status_code == 200
    assert r.json()["changed"] is True
    assert r.json()["version"] == 3
    assert r.json()["minimum"] == "35.000000000"
    # 相同值：无实际变更，version 保持 3
    r = _put_retention(client, "V1", "S1", "35", 3)
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert r.json()["version"] == 3
    assert r.json()["minimum"] == "35.000000000"
    assert _status(client, "V1")["version"] == 3


def test_clear_rule_bumps_version_clearing_missing_rule_is_noop(client):
    _mix_v1(client)
    _put_retention(client, "V1", "S1", "30", 1)
    assert _status(client, "V1")["version"] == 2
    # 解除存在的规则：version +1
    r = _put_retention(client, "V1", "S1", None, 2)
    assert r.status_code == 200
    assert r.json()["changed"] is True
    assert r.json()["version"] == 3
    assert r.json()["minimum"] is None
    assert r.json()["rules"] == []
    assert _get_retention(client, "V1").json()["rules"] == []
    # 再次解除（本不存在）：无变更的成功调用
    r = _put_retention(client, "V1", "S1", None, 3)
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert r.json()["version"] == 3
    assert _status(client, "V1")["version"] == 3


def test_get_rules_sorted_with_live_current_amounts(client):
    _mix_v1(client)
    _put_retention(client, "V1", "S2", "10", 1)
    _put_retention(client, "V1", "S1", "5", 2)
    body = _get_retention(client, "V1").json()
    assert [r["source_id"] for r in body["rules"]] == ["S1", "S2"]
    assert body["rules"][0] == {
        "source_id": "S1",
        "minimum": "5.000000000",
        "current_amount": "40.000000000",
    }
    assert body["rules"][1] == {
        "source_id": "S2",
        "minimum": "10.000000000",
        "current_amount": "60.000000000",
    }
    # 空容器从未设置规则
    container(client, "E1")
    assert _get_retention(client, "E1").json() == {
        "container_id": "E1",
        "version": 0,
        "rules": [],
    }


def test_retention_change_does_not_touch_revision_or_audit(client):
    _mix_v1(client)
    assert _revision(client) == 1
    assert client.get("/audit/records").json()["count"] == 1
    token = client.get("/sources/S1/containers", params={"limit": 1}).json()[
        "next_cursor"
    ]

    _put_retention(client, "V1", "S1", "30", 1)
    _put_retention(client, "V1", "S1", "30", 2)  # 无变更
    _put_retention(client, "V1", "S1", None, 2)  # 解除

    # 留存规则变更不是转移 / 核销：修订号、审计记录、来源反查游标均不受影响
    assert _revision(client) == 1
    assert client.get("/audit/records").json()["count"] == 1
    r = client.get("/sources/S1/containers", params={"limit": 1, "cursor": token})
    assert r.status_code == 200, r.text


def test_put_retention_unknown_container_404(client):
    sample(client, "S1", "100")
    r = _put_retention(client, "GHOST", "S1", "1", 0)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_container"
    assert _get_retention(client, "GHOST").status_code == 404


def test_put_retention_source_must_be_registered_sample(client):
    _mix_v1(client)
    container(client, "E1")
    # 未知 ID
    r = _put_retention(client, "V1", "GHOST", "1", 1)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "unknown_sample"
    assert err["details"] == {"unknown_samples": ["GHOST"]}
    # 已登记但只是空容器：同样拒绝
    r = _put_retention(client, "V1", "E1", "1", 1)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unknown_sample"
    # 拒绝后规则与版本均不变
    assert _get_retention(client, "V1").json()["rules"] == []
    assert _status(client, "V1")["version"] == 1


def test_put_retention_above_current_share_rejected(client):
    _mix_v1(client)
    # V1 中 S1 当前 40：下限 40.000000001 超过当前份额
    r = _put_retention(client, "V1", "S1", "40.000000001", 1)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "retention_above_share"
    assert err["details"] == {
        "container_id": "V1",
        "source_id": "S1",
        "minimum": "40.000000001",
        "current_amount": "40.000000000",
    }
    # 已登记但未混入 V1 的样品：当前份额 0，任何正值下限都超过
    sample(client, "S9", "1")
    r = _put_retention(client, "V1", "S9", "1", 1)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "retention_above_share"
    assert r.json()["error"]["details"]["current_amount"] == "0.000000000"
    # 规则、版本不变
    assert _status(client, "V1")["version"] == 1
    assert _get_retention(client, "V1").json()["rules"] == []


def test_put_retention_equal_to_current_share_allowed(client):
    _mix_v1(client)
    r = _put_retention(client, "V1", "S1", "40", 1)
    assert r.status_code == 200, r.text
    assert r.json()["rules"][0]["minimum"] == "40.000000000"
    # 此后任何把 S1 扣出 V1 的正量转移都应被拒绝（在批量校验中验证）
    container(client, "V2")
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "V1", "destination": "V2", "volume": "1"}]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "retention_below_minimum"


def test_put_retention_invalid_values_rejected(client):
    _mix_v1(client)
    # 0 / 负 / 超精度：业务层 422 invalid_volume（错误信封）
    for bad in ("0", "-1", "1.0000000001"):
        r = _put_retention(client, "V1", "S1", bad, 1)
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["error"]["code"] == "invalid_volume", bad
    # 非有限数：schema 层同样拒绝为 422（与其它接口的体积字段一致）
    for bad in ("NaN", "Infinity"):
        assert _put_retention(client, "V1", "S1", bad, 1).status_code == 422, bad
    # 任何失败都不留痕、不加版本
    assert _status(client, "V1")["version"] == 1
    assert _get_retention(client, "V1").json()["rules"] == []


def test_put_retention_stale_version_rejected_and_unchanged(client):
    _mix_v1(client)
    r = _put_retention(client, "V1", "S1", "30", 0)  # 当前版本已是 1
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_container"
    assert _status(client, "V1")["version"] == 1
    assert _get_retention(client, "V1").json()["rules"] == []


def test_put_retention_schema_level_validation(client):
    _mix_v1(client)
    # 缺少 minimum / expected_version
    r = client.put("/containers/V1/retention/S1", json={"expected_version": 1})
    assert r.status_code == 422
    r = client.put("/containers/V1/retention/S1", json={"minimum": "1"})
    assert r.status_code == 422
    # 布尔 / 负数版本
    assert _put_retention(client, "V1", "S1", "1", True).status_code == 422
    assert _put_retention(client, "V1", "S1", "1", -1).status_code == 422
    assert _status(client, "V1")["version"] == 1


def test_retention_version_bump_makes_expected_versions_stale(client):
    """锁定使容器 version +1：之后批量提交携带旧 expected_version 会 409。"""
    _mix_v1(client)
    container(client, "V2")
    assert _put_retention(client, "V1", "S1", "10", 1).status_code == 200
    body = {
        "transfers": [{"source": "V1", "destination": "V2", "volume": "1"}],
        "expected_versions": {"V1": 1, "V2": 0},
    }
    r = client.post("/transfers/batch", json=body)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_container"
    # 用新版本（且留存份额允许）重试成功
    body["expected_versions"]["V1"] = 2
    r = client.post("/transfers/batch", json=body)
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# 批量转移 / 预演的留存校验
# --------------------------------------------------------------------------- #
def test_batch_below_retention_rejects_whole_batch_with_details(client):
    _mix_v1(client)
    container(client, "V2")
    _put_retention(client, "V1", "S1", "30", 1)
    # V1=100(S1=40,S2=60)，取 30：S1 被扣 12 -> 28 < 30，整批拒绝
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "V1", "destination": "V2", "volume": "30"}]},
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "retention_below_minimum"
    assert "第 1 条转移" in err["message"]
    assert "V1" in err["message"]
    assert err["details"] == {
        "index": 1,
        "source": "V1",
        "violations": [
            {
                "source_id": "S1",
                "remaining": "28.000000000",
                "minimum": "30.000000000",
                "shortfall": "2.000000000",
            }
        ],
    }
    # 整批不落库：份额、版本、修订号与审计链均不变
    status = _status(client, "V1")
    assert status["version"] == 2  # 锁定时 +1，转移失败不再 +1
    assert status["volume"] == "100.000000000"
    assert volume_of(client, "V2") == "0"
    assert _revision(client) == 1
    assert client.get("/audit/records").json()["count"] == 1


def test_batch_transfer_exactly_to_floor_allowed(client):
    _mix_v1(client)
    container(client, "V2")
    _put_retention(client, "V1", "S1", "28", 1)
    # 取 30：S1 恰好扣到 28（等于下限）允许
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "V1", "destination": "V2", "volume": "30"}]},
    )
    assert r.status_code == 200, r.text
    sources = {s["source_id"]: s["amount"] for s in _status(client, "V1")["sources"]}
    assert sources["S1"] == "28.000000000"
    # 此后 S1 恰好处于下限：任何把 S1 扣出正量的转移都必须拒绝。
    # 取 1：按 28/42 比例分摊 S1=0.4 -> S1 剩 27.6 < 28，整批拒绝且不落库。
    before = _status(client, "V1")["version"]
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "V1", "destination": "V2", "volume": "1"}]},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "retention_below_minimum"
    assert _status(client, "V1")["version"] == before
    assert volume_of(client, "V1") == "70.000000000"


def test_batch_violation_at_second_step_reports_index_and_persists_nothing(client):
    _mix_v1(client)
    container(client, "V2")
    _put_retention(client, "V1", "S1", "30", 1)
    # 第一步取 5（S1=38，合法），第二步取 30（S1 再扣 12 -> 26，违约）
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "V1", "destination": "V2", "volume": "5"},
                {"source": "V1", "destination": "V2", "volume": "30"},
            ]
        },
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "retention_below_minimum"
    assert err["details"]["index"] == 2
    assert err["details"]["source"] == "V1"
    assert err["details"]["violations"][0]["remaining"] == "26.000000000"
    # 连第一步也不落地
    assert volume_of(client, "V1") == "100.000000000"
    assert volume_of(client, "V2") == "0"
    assert _status(client, "V1")["version"] == 2


def test_batch_roundtrip_rejected_even_though_later_step_restores(client):
    """先转出使份额低于下限，即使同批后续步骤会转回，也在违约那一步整批拒绝。"""
    _mix_v1(client)
    container(client, "V2")
    _put_retention(client, "V1", "S1", "30", 1)
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "V1", "destination": "V2", "volume": "30"},  # S1 -> 28 违约
                {"source": "V2", "destination": "V1", "volume": "30"},  # 试图转回
            ]
        },
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "retention_below_minimum"
    assert err["details"]["index"] == 1
    assert "即使后续步骤会转回" in err["message"]
    assert volume_of(client, "V1") == "100.000000000"
    assert volume_of(client, "V2") == "0"


def test_batch_reports_all_violating_sources_sorted(client):
    """源容器多条留存规则同时被突破：details 列出全部违约来源（按 source_id）。"""
    _mix_v1(client)
    container(client, "V2")
    _put_retention(client, "V1", "S2", "50", 1)
    _put_retention(client, "V1", "S1", "30", 2)
    # 取 30：S1 -> 28（差 2）、S2 -> 42（差 8），双双违约
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "V1", "destination": "V2", "volume": "30"}]},
    )
    assert r.status_code == 422
    violations = r.json()["error"]["details"]["violations"]
    assert [v["source_id"] for v in violations] == ["S1", "S2"]
    assert violations[0] == {
        "source_id": "S1",
        "remaining": "28.000000000",
        "minimum": "30.000000000",
        "shortfall": "2.000000000",
    }
    assert violations[1]["remaining"] == "42.000000000"
    assert violations[1]["minimum"] == "50.000000000"
    assert violations[1]["shortfall"] == "8.000000000"


def test_retention_only_constrains_source_not_incoming_destination(client):
    """留存规则只保护“转出”：向设有规则的容器继续转入不受约束。"""
    _mix_v1(client)
    _put_retention(client, "V1", "S1", "30", 1)
    # S1 再转入 10：V1 的 S1 增加到 50，规则当然满足，转入不受阻
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "10"}]},
    )
    assert r.status_code == 200, r.text
    assert _rules(client, "V1")["S1"]["current_amount"] == "50.000000000"
    # 但源容器 S1 自身没有留存规则，转出不受阻
    assert volume_of(client, "S1") == "50.000000000"


def test_preview_enforces_retention_identically_and_persists_nothing(client):
    _mix_v1(client)
    container(client, "V2")
    _put_retention(client, "V1", "S1", "30", 1)
    body = {"transfers": [{"source": "V1", "destination": "V2", "volume": "30"}]}

    r_preview = client.post("/transfers/preview", json=body)
    assert r_preview.status_code == 422
    ep = r_preview.json()["error"]
    assert ep["code"] == "retention_below_minimum"
    assert ep["details"] == {
        "index": 1,
        "source": "V1",
        "violations": [
            {
                "source_id": "S1",
                "remaining": "28.000000000",
                "minimum": "30.000000000",
                "shortfall": "2.000000000",
            }
        ],
    }

    # 预演不落库：同一请求真实提交时返回同样的错误码 / details
    r_batch = client.post("/transfers/batch", json=body)
    assert r_batch.status_code == 422
    assert r_batch.json()["error"]["code"] == ep["code"]
    assert r_batch.json()["error"]["details"] == ep["details"]

    # 合法预演：给出预测状态但数据库不变
    ok_body = {"transfers": [{"source": "V1", "destination": "V2", "volume": "25"}]}
    r = client.post("/transfers/preview", json=ok_body)
    assert r.status_code == 200, r.text
    predicted = {c["container_id"]: c for c in r.json()["containers"]}
    assert predicted["V1"]["sources"][0]["amount"] == "30.000000000"
    assert predicted["V1"]["version"] == 3
    assert volume_of(client, "V1") == "100.000000000"
    assert _status(client, "V1")["version"] == 2
    assert _revision(client) == 1


def test_preview_with_expected_versions_still_enforces_retention(client):
    _mix_v1(client)
    container(client, "V2")
    _put_retention(client, "V1", "S1", "30", 1)  # V1 -> version 2
    r = client.post(
        "/transfers/preview",
        json={
            "transfers": [{"source": "V1", "destination": "V2", "volume": "30"}],
            "expected_versions": {"V1": 2, "V2": 0},
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "retention_below_minimum"
    # 版本过期优先于留存校验（版本在状态读入时即比对）
    r = client.post(
        "/transfers/preview",
        json={
            "transfers": [{"source": "V1", "destination": "V2", "volume": "30"}],
            "expected_versions": {"V1": 1, "V2": 0},
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_container"


def test_retention_failure_with_key_writes_no_idempotency_record(client):
    _mix_v1(client)
    container(client, "V2")
    _put_retention(client, "V1", "S1", "30", 1)
    body = {
        "transfers": [{"source": "V1", "destination": "V2", "volume": "30"}],
        "idempotency_key": "retention-key-1",
    }
    r = client.post("/transfers/batch", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "retention_below_minimum"
    conn = connect()
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0
        )
    finally:
        conn.close()
    # 失败不留痕：同一个键可随合法请求体重试成功
    body["transfers"][0]["volume"] = "25"
    r = client.post("/transfers/batch", json=body)
    assert r.status_code == 200, r.text
    # 再改回会违约的请求体：同键异请求体 -> 409（键已被成功请求占用）
    body["transfers"][0]["volume"] = "30"
    r = client.post("/transfers/batch", json=body)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"


def test_idempotent_replay_not_revalidated_against_new_retention_rule(client):
    """已成功批次的重放直接返回首次结果，不会用新留存规则重新校验。"""
    _mix_v1(client)
    container(client, "V2")
    body = {
        "transfers": [{"source": "V1", "destination": "V2", "volume": "30"}],
        "idempotency_key": "retention-key-2",
    }
    r = client.post("/transfers/batch", json=body)
    assert r.status_code == 200  # 首次成功：S1 被扣到 28
    # 事后把 S1 下限锁到 30（当前份额 28 < 30）——设置接口本身拒绝高于当前份额
    r = _put_retention(client, "V1", "S1", "30", 2)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "retention_above_share"
    # 改为锁 20（合法）；重放旧批次仍返回首次结果、不再扣减、不再加版本
    assert _put_retention(client, "V1", "S1", "20", 2).status_code == 200
    r = client.post("/transfers/batch", json=body)
    assert r.status_code == 200
    assert r.headers.get("idempotency-replayed") == "true"
    assert r.json()["versions"] == {"V1": 2, "V2": 1}
    assert volume_of(client, "V1") == "70.000000000"


def test_retention_uses_fixed_point_apportionment_at_boundary(client):
    """极小体积刻度下按最大余额法扣减：差一个刻度即整批拒绝，份额不被改动。"""
    sample(client, "S1", "1")
    sample(client, "S2", "1")
    container(client, "V1")
    container(client, "V2")
    client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "1"},
                {"source": "S2", "destination": "V1", "volume": "1"},
            ]
        },
    )
    # V1: S1=1, S2=1；锁 S1=0.999999999
    _put_retention(client, "V1", "S1", "0.999999999", 1)
    # 取 1 个刻度：最大余额法把它分给 S1（平局按 ID），S1 -> 0.999999999 恰好等于下限
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "V1", "destination": "V2", "volume": "0.000000001"}
            ]
        },
    )
    assert r.status_code == 200, r.text
    sources = {s["source_id"]: s["amount"] for s in _status(client, "V1")["sources"]}
    assert sources["S1"] == "0.999999999"
    assert sources["S2"] == "1.000000000"
    # 再取 1 个刻度（分给 S2，S1 不动）：S1 保持下限，仍允许
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "V1", "destination": "V2", "volume": "0.000000001"}
            ]
        },
    )
    assert r.status_code == 200, r.text
    # 再取 1 个刻度必从 S1 扣 -> 低于下限
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "V1", "destination": "V2", "volume": "0.000000001"}
            ]
        },
    )
    assert r.status_code == 422
    details = r.json()["error"]["details"]
    assert details["violations"] == [
        {
            "source_id": "S1",
            "remaining": "0.999999998",
            "minimum": "0.999999999",
            "shortfall": "0.000000001",
        }
    ]


# --------------------------------------------------------------------------- #
# 损耗核销受同一约束
# --------------------------------------------------------------------------- #
def test_loss_below_retention_rejected_and_nothing_changes(client):
    _mix_v1(client)
    _put_retention(client, "V1", "S1", "30", 1)  # V1 -> v2
    # 核销到 70（损耗 30）：S1 扣 12 -> 28 < 30
    r = client.post(
        "/containers/V1/losses",
        json={"measured_volume": "70", "expected_version": 2},
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "retention_below_minimum"
    assert "V1" in err["message"]
    assert err["details"] == {
        "container_id": "V1",
        "violations": [
            {
                "source_id": "S1",
                "remaining": "28.000000000",
                "minimum": "30.000000000",
                "shortfall": "2.000000000",
            }
        ],
    }
    # 组件、版本、修订号与损耗记录均不变
    assert volume_of(client, "V1") == "100.000000000"
    assert _status(client, "V1")["version"] == 2
    assert _revision(client) == 1
    assert client.get("/containers/V1/losses").json()["count"] == 0


def test_loss_full_writeoff_blocked_by_retention(client):
    _mix_v1(client)
    _put_retention(client, "V1", "S1", "1", 1)
    r = client.post(
        "/containers/V1/losses",
        json={"measured_volume": "0", "expected_version": 2},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "retention_below_minimum"
    assert volume_of(client, "V1") == "100.000000000"


def test_loss_exactly_to_floor_allowed_and_clearing_allows_writeoff(client):
    _mix_v1(client)
    _put_retention(client, "V1", "S1", "30", 1)
    # 核销到 75（损耗 25）：S1 扣 10 -> 30 恰好等于下限，允许
    r = client.post(
        "/containers/V1/losses",
        json={"measured_volume": "75", "expected_version": 2},
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 3
    sources = {s["source_id"]: s["amount"] for s in _status(client, "V1")["sources"]}
    assert sources["S1"] == "30.000000000"
    assert sources["S2"] == "45.000000000"
    assert _revision(client) == 2

    # 零损耗不受留存约束（没有任何来源被扣减），但照常递增版本
    r = client.post(
        "/containers/V1/losses",
        json={"measured_volume": "75", "expected_version": 3},
    )
    assert r.status_code == 200, r.text
    assert r.json()["deductions"] == []
    assert r.json()["version"] == 4

    # 解除规则后整容器核销为 0 允许
    assert _put_retention(client, "V1", "S1", None, 4).json()["version"] == 5
    r = client.post(
        "/containers/V1/losses",
        json={"measured_volume": "0", "expected_version": 5},
    )
    assert r.status_code == 200, r.text
    assert _status(client, "V1")["sources"] == []


def test_failed_loss_under_retention_keeps_revision_and_cursor(client):
    _mix_v1(client)
    _put_retention(client, "V1", "S1", "30", 1)
    token = client.get("/sources/S1/containers", params={"limit": 1}).json()[
        "next_cursor"
    ]
    r = client.post(
        "/containers/V1/losses",
        json={"measured_volume": "0", "expected_version": 2},
    )
    assert r.status_code == 422
    # 修订号未变 -> 旧游标继续有效
    r = client.get("/sources/S1/containers", params={"limit": 1, "cursor": token})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# 序列化与迁移
# --------------------------------------------------------------------------- #
def test_decimal_fields_serialize_as_strings(client):
    _mix_v1(client)
    _put_retention(client, "V1", "S1", "30", 1)
    body = _get_retention(client, "V1").json()
    assert isinstance(body["rules"][0]["minimum"], str)
    assert isinstance(body["rules"][0]["current_amount"], str)


def test_init_db_migrates_legacy_db_without_retention_table(tmp_path, monkeypatch):
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
        assert "retention_rules" in tables

        # 旧库迁移后可直接使用留存接口（旧容器版本视为 0；OLD 是自指样品）
        out = service.set_container_retention(
            conn, "OLD", "OLD", Decimal("4"), 0
        )
        assert out["changed"] is True
        assert out["version"] == 1
        assert out["rules"][0]["current_amount"] == "5"

        # 转移受规则约束：建一个目标后从 OLD 取 2 -> OLD 剩 3 < 4，拒绝且不变
        service.register_empty_container(conn, "DST")
        try:
            service.apply_transfers(conn, [("OLD", "DST", Decimal("2"))])
        except service.LineageError as exc:
            assert exc.status_code == 422
            assert exc.code == "retention_below_minimum"
            assert exc.details["source"] == "OLD"
            assert exc.details["violations"][0]["remaining"] == "3.000000000"
        else:
            raise AssertionError("留存违约应当拒绝整批转移")
        # 旧库原始存储为未补零的字符串 "5"，用 Decimal 比较规范化后的体积
        assert Decimal(service.get_container_status(conn, "OLD")["volume"]) == Decimal(
            "5"
        )

        # 取 1 恰好剩 4（等于下限）允许
        applied, versions = service.apply_transfers(
            conn, [("OLD", "DST", Decimal("1"))]
        )
        assert applied == 1
        assert versions["OLD"] == 2
        assert service.get_container_status(conn, "OLD")["volume"] == "4.000000000"
    finally:
        conn.close()
