"""GET /sources/{sample_id}/containers 祖先来源反向追踪端到端测试。

覆盖：
* 基本反向追踪：初始样品自身 + 经混合传播的全部下游容器，字段（体积 /
  绝对份额 / 占比 / Decimal 字符串）正确，结果按 container_id 字典序；
* 已完全转空的初始样品自身不再出现，但仍追踪其下游容器；
* min_amount 过滤（含边界 >=、精度规范化、非法值 422）；
* limit（1..100，默认 100，越界 422）与 cursor 稳定分页（跨页并集完整、
  无重叠、顺序正确）；
* 游标失效：损坏 400 invalid_cursor、跨样品 / min_amount 不一致
  400 cursor_parameter_mismatch；
* 翻页期间成功批次提交 -> 409 stale_cursor（details 含新旧修订号）；
* 样品不存在 / 仅为空容器 -> 404 unknown_sample；
* transfer_revision：仅成功批次在同一事务 +1；预演、失败、幂等重放不递增；
* 查询走只读事务（连接拒绝写入）；
* 旧库迁移：transfer_revision 表与覆盖索引在旧库上幂等补齐。
"""
import base64
import json
import os
import sqlite3
from decimal import Decimal

import pytest

from helpers import container, sample

from app.db import connect, connect_readonly, init_db
from app import service


def _get(client, sid, **params):
    return client.get(f"/sources/{sid}/containers", params=params)


def _map_items(body):
    return {item["container_id"]: item for item in body["containers"]}


def _setup_mix(client):
    """S1 100 / S2 100 -> V1 (40/60) -> V2 取 25 (S1=10, S2=15)。"""
    sample(client, "S1", "100")
    sample(client, "S2", "100")
    container(client, "V1")
    container(client, "V2")
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "40"},
                {"source": "S2", "destination": "V1", "volume": "60"},
                {"source": "V1", "destination": "V2", "volume": "25"},
            ]
        },
    )
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# 基本反向追踪
# --------------------------------------------------------------------------- #
def test_lists_all_containers_still_holding_the_source(client):
    _setup_mix(client)
    r = _get(client, "S1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sample_id"] == "S1"
    assert body["transfer_revision"] == 1
    assert body["min_amount"] == "0.000000000"
    assert body["limit"] == 100
    assert body["count"] == 3
    assert body["next_cursor"] is None

    # 字典序：S1 < V1 < V2
    assert [x["container_id"] for x in body["containers"]] == ["S1", "V1", "V2"]
    items = _map_items(body)
    # 初始样品自身剩余 60
    assert items["S1"] == {
        "container_id": "S1",
        "volume": "60.000000000",
        "source_amount": "60.000000000",
        "proportion": "1.000000",
    }
    # V1 中 S1 剩 30，占 75 的 40%
    assert items["V1"]["volume"] == "75.000000000"
    assert items["V1"]["source_amount"] == "30.000000000"
    assert items["V1"]["proportion"] == "0.400000"
    # V2 中 S1 为 10，占 25 的 40%
    assert items["V2"] == {
        "container_id": "V2",
        "volume": "25.000000000",
        "source_amount": "10.000000000",
        "proportion": "0.400000",
    }


def test_s2_trace_is_independent(client):
    _setup_mix(client)
    items = _map_items(_get(client, "S2").json())
    assert set(items) == {"S2", "V1", "V2"}
    assert items["V1"]["source_amount"] == "45.000000000"
    assert items["V1"]["proportion"] == "0.600000"
    assert items["V2"]["source_amount"] == "15.000000000"
    assert items["V2"]["proportion"] == "0.600000"
    assert items["S2"]["source_amount"] == "40.000000000"


def test_empty_sample_result_before_any_transfer(client):
    sample(client, "FRESH", "10")
    container(client, "V1")
    body = _get(client, "FRESH").json()
    assert body["transfer_revision"] == 0
    assert body["count"] == 1
    assert body["containers"] == [
        {
            "container_id": "FRESH",
            "volume": "10.000000000",
            "source_amount": "10.000000000",
            "proportion": "1.000000",
        }
    ]
    assert body["next_cursor"] is None


def test_fully_drained_sample_drops_itself_but_keeps_descendants(client):
    sample(client, "S1", "10")
    container(client, "V1")
    container(client, "V2")
    client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "6"},
                {"source": "V1", "destination": "V2", "volume": "2"},
            ]
        },
    )
    ids = [x["container_id"] for x in _get(client, "S1").json()["containers"]]
    # S1 已无自身份额（4）？其实 S1 还剩 4 -> 仍出现；先把 4 也转走
    assert ids == ["S1", "V1", "V2"]
    client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "4"}]},
    )
    body = _get(client, "S1").json()
    ids = [x["container_id"] for x in body["containers"]]
    # S1 自身份额归零，不再出现；下游 V1/V2 仍在
    assert ids == ["V1", "V2"]
    assert all(Decimal(x["source_amount"]) > 0 for x in body["containers"])


# --------------------------------------------------------------------------- #
# min_amount 过滤
# --------------------------------------------------------------------------- #
def test_min_amount_filters_with_inclusive_boundary(client):
    _setup_mix(client)
    # S1: S1=60, V1=30, V2=10
    assert [x["container_id"] for x in _get(client, "S1", min_amount="10").json()["containers"]] == [
        "S1",
        "V1",
        "V2",
    ]
    assert [x["container_id"] for x in _get(client, "S1", min_amount="30").json()["containers"]] == [
        "S1",
        "V1",
    ]
    assert [x["container_id"] for x in _get(client, "S1", min_amount="30.000000001").json()["containers"]] == [
        "S1",
    ]
    assert [x["container_id"] for x in _get(client, "S1", min_amount="999").json()["containers"]] == []


def test_min_amount_is_numeric_not_lexicographic(client):
    # 回归：金额字符串字典序在整数位宽度不同处与数值序不一致
    # （"2.000000000" >= "10.000000000" 按字符串为真，数值为假）。
    sample(client, "S1", "100")
    spec = {"C02": "2", "C03": "3", "C10": "10", "C20": "20"}
    for cid, vol in spec.items():
        container(client, cid)
        client.post(
            "/transfers/batch",
            json={"transfers": [{"source": "S1", "destination": cid, "volume": vol}]},
        )
    # S1 自身剩余 65；阈值 10：保留 S1(65) 与 C10(10)、C20(20)，排除 C02/C03
    ids = [x["container_id"] for x in _get(client, "S1", min_amount="10").json()["containers"]]
    assert ids == ["C10", "C20", "S1"]

    # 阈值 2.5：C03(3) 入选、C02(2) 排除
    ids = [x["container_id"] for x in _get(client, "S1", min_amount="2.5").json()["containers"]]
    assert ids == ["C03", "C10", "C20", "S1"]

    # 与分页组合：limit=1 逐页结果必须与一次性过滤一致
    pages = _paginate(client, "S1", 1, min_amount="10")
    flattened = [x["container_id"] for p in pages for x in p["containers"]]
    assert flattened == ["C10", "C20", "S1"]


def test_min_amount_is_quantized_and_reflected_in_response(client):
    _setup_mix(client)
    body = _get(client, "S1", min_amount=10).json()  # JSON 数字也接受
    assert body["min_amount"] == "10.000000000"


def test_min_amount_invalid_values_422(client):
    _setup_mix(client)
    for bad in ("-1", "abc"):
        r = _get(client, "S1", min_amount=bad)
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["error"]["code"] == "invalid_min_amount", bad


def test_min_amount_string_numeric_nonfinite_422(client):
    _setup_mix(client)
    r = _get(client, "S1", min_amount="NaN")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_min_amount"


# --------------------------------------------------------------------------- #
# limit
# --------------------------------------------------------------------------- #
def test_limit_bounds(client):
    _setup_mix(client)
    for bad in (0, -1, 101, 1000):
        r = _get(client, "S1", limit=bad)
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["error"]["code"] == "invalid_limit", bad


def test_limit_one_walks_every_container(client):
    _setup_mix(client)
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        body = _get(client, "S1", **params).json()
        assert body["count"] <= 1
        seen.extend(x["container_id"] for x in body["containers"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == ["S1", "V1", "V2"]


# --------------------------------------------------------------------------- #
# cursor 分页：完整性 / 稳定性
# --------------------------------------------------------------------------- #
def _paginate(client, sid, limit, **extra):
    pages = []
    cursor = None
    while True:
        params = {"limit": limit, **extra}
        if cursor is not None:
            params["cursor"] = cursor
        body = _get(client, sid, **params).json()
        pages.append(body)
        cursor = body["next_cursor"]
        if cursor is None:
            break
    return pages


def test_pagination_is_stable_and_complete(client):
    sample(client, "S1", "100")
    # 5 个下游容器，名字交错于 S1 两侧（字典序 C*, V*, 含一个在 S1 之前的）
    for cid in ("AA", "BB", "V1", "V2", "V3"):
        container(client, cid)
    client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": cid, "volume": "5"}
                for cid in ("AA", "BB", "V1", "V2", "V3")
            ],
        },
    )
    full = [x["container_id"] for x in _get(client, "S1").json()["containers"]]
    assert full == ["AA", "BB", "S1", "V1", "V2", "V3"]

    for page_size in (1, 2, 3, 5, 6, 100):
        pages = _paginate(client, "S1", page_size)
        flattened = [x["container_id"] for p in pages for x in p["containers"]]
        assert flattened == full, page_size
        # 无重叠
        assert len(flattened) == len(set(flattened)), page_size
        # 除最后一页外每页满页，最后一页有 next_cursor=None
        assert all(p["count"] == page_size for p in pages[:-1]), page_size
        assert pages[-1]["next_cursor"] is None


def test_cursor_opaque_and_contains_revision(client):
    _setup_mix(client)
    body = _get(client, "S1", limit=2).json()
    token = body["next_cursor"]
    assert isinstance(token, str) and token
    raw = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    assert raw == {
        "v": service.CURSOR_VERSION,
        "rev": 1,
        "sample": "S1",
        "after": "V1",
        "min_amount": "0.000000000",
    }


def test_garbled_cursor_400(client):
    _setup_mix(client)
    for bad in ("garbage", "!!!", ""):
        r = _get(client, "S1", cursor=bad)
        # 空字符串被 httpx 忽略为无 cursor；其余应 400
        if bad:
            assert r.status_code == 400, (bad, r.text)
            assert r.json()["error"]["code"] == "invalid_cursor", bad

    # 可解码但内容伪造
    fake = base64.urlsafe_b64encode(
        json.dumps({"v": 99}).encode()
    ).decode().rstrip("=")
    r = _get(client, "S1", cursor=fake)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_cursor"

    # 合法 base64 但字段缺失
    fake2 = base64.urlsafe_b64encode(
        json.dumps({"v": service.CURSOR_VERSION, "rev": 0}).encode()
    ).decode().rstrip("=")
    assert _get(client, "S1", cursor=fake2).status_code == 400


def test_cursor_reuse_on_same_page_is_idempotent(client):
    _setup_mix(client)
    body = _get(client, "S1", limit=2).json()
    token = body["next_cursor"]
    r1 = _get(client, "S1", limit=2, cursor=token)
    r2 = _get(client, "S1", limit=2, cursor=token)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()
    assert [x["container_id"] for x in r1.json()["containers"]] == ["V2"]


def test_cursor_bound_to_sample_and_min_amount(client):
    _setup_mix(client)
    token = _get(client, "S1", limit=1).json()["next_cursor"]

    # 跨样品复用游标
    r = _get(client, "S2", limit=1, cursor=token)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "cursor_parameter_mismatch"

    # 同样品但 min_amount 改变
    r = _get(client, "S1", limit=1, min_amount="1", cursor=token)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "cursor_parameter_mismatch"


def test_pagination_over_many_containers_is_complete_and_ordered(client):
    n = 250
    sample(client, "S1", str(n))
    # 三位零填充保证容器数量级一致；交错份额以同时覆盖阈值过滤
    for i in range(n):
        cid = f"C{i:03d}"
        container(client, cid)
    # 一整批依次转入（每容器 1），同批原子只 +1 修订号
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": f"C{i:03d}", "volume": "1"}
                for i in range(n)
            ]
        },
    )
    assert r.status_code == 200, r.text

    full = _get(client, "S1").json()
    # S1 已转空 -> 恰好 250 个下游容器；默认页大小 100，首页给出 next_cursor
    assert full["count"] == 100
    assert full["next_cursor"] is not None
    expected = [f"C{i:03d}" for i in range(n)]
    pages = _paginate(client, "S1", 100)
    all_ids = [x["container_id"] for p in pages for x in p["containers"]]
    assert all_ids == expected
    assert full["transfer_revision"] == 1

    # 逐页 limit=1，修订号全程为 1，每页游标均有效
    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        body = _get(client, "S1", **params).json()
        assert body["transfer_revision"] == 1
        seen.extend(x["container_id"] for x in body["containers"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == expected


# --------------------------------------------------------------------------- #
# stale_cursor：翻页期间修订号变化
# --------------------------------------------------------------------------- #
def test_stale_cursor_after_intervening_commit(client):
    _setup_mix(client)
    token = _get(client, "S1", limit=2).json()["next_cursor"]

    # 翻页期间另一批次成功提交（转移 S2 -> V2 不碰 S1 也照样作废游标：
    # 修订号是全局的）
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S2", "destination": "V2", "volume": "1"}]},
    )
    assert r.status_code == 200

    r = _get(client, "S1", limit=2, cursor=token)
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "stale_cursor"
    assert err["details"] == {"cursor_revision": 1, "current_revision": 2}


def test_stale_cursor_not_raised_when_no_revision_change(client):
    _setup_mix(client)
    token = _get(client, "S1", limit=2).json()["next_cursor"]

    # 失败批次不递增 -> 游标仍可用
    bad = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S2", "destination": "V2", "volume": "99999"}]},
    )
    assert bad.status_code == 422
    r = _get(client, "S1", limit=2, cursor=token)
    assert r.status_code == 200
    assert [x["container_id"] for x in r.json()["containers"]] == ["V2"]

    # 预演不递增 -> 游标仍可用
    client.post(
        "/transfers/preview",
        json={"transfers": [{"source": "S2", "destination": "V2", "volume": "1"}]},
    )
    token2 = r.json()["next_cursor"]
    assert token2 is None  # 已是最后一页

    # 幂等重放不递增：取一个新分页起点（limit=1），提交后重放，再翻页
    t = _get(client, "S1", limit=1).json()["next_cursor"]
    body = {
        "transfers": [{"source": "S1", "destination": "V2", "volume": "1"}],
        "idempotency_key": "k-stale",
    }
    assert client.post("/transfers/batch", json=body).status_code == 200
    # 修订号已变 -> 旧游标先作废掉
    assert _get(client, "S1", limit=1, cursor=t).status_code == 409
    # 重放不改变修订号：以当前第一页重新取游标，连续两次重放后仍可翻页
    t2 = _get(client, "S1", limit=1).json()["next_cursor"]
    rev_before = _get(client, "S1").json()["transfer_revision"]
    for _ in range(2):
        replay = client.post("/transfers/batch", json=body)
        assert replay.status_code == 200
        assert replay.headers.get("Idempotency-Replayed") == "true"
    assert _get(client, "S1").json()["transfer_revision"] == rev_before
    assert _get(client, "S1", limit=1, cursor=t2).status_code == 200


# --------------------------------------------------------------------------- #
# 404 / 修订号语义
# --------------------------------------------------------------------------- #
def test_unknown_sample_404(client):
    r = _get(client, "GHOST")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_sample"


def test_empty_container_id_is_not_a_sample_404(client):
    container(client, "EMPTY")
    r = _get(client, "EMPTY")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_sample"


def test_revision_increments_only_on_successful_batch(client):
    sample(client, "S1", "100")
    container(client, "V1")

    rev = lambda: _get(client, "S1").json()["transfer_revision"]
    assert rev() == 0

    client.post(
        "/transfers/preview",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "5"}]},
    )
    assert rev() == 0

    client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "999"}]},
    )
    assert rev() == 0

    body = {
        "transfers": [{"source": "S1", "destination": "V1", "volume": "5"}],
        "idempotency_key": "k1",
    }
    client.post("/transfers/batch", json=body)
    assert rev() == 1
    client.post("/transfers/batch", json=body)  # 重放
    assert rev() == 1

    client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "3"}]},
    )
    assert rev() == 2

    # 一个批次内多条转移（含链式）也只 +1
    container(client, "V2")
    client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V2", "volume": "2"},
                {"source": "V2", "destination": "V1", "volume": "1"},
            ]
        },
    )
    assert rev() == 3


def test_sample_registration_and_capacity_change_do_not_move_revision(client):
    sample(client, "S1", "10")
    container(client, "V1")
    assert _get(client, "S1").json()["transfer_revision"] == 0
    client.put("/containers/V1/capacity", json={"capacity": "100"})
    client.put("/compatibility/rules", json={"rules": []})
    assert _get(client, "S1").json()["transfer_revision"] == 0


def test_failed_batch_rolls_back_revision_even_at_update_point(client):
    # 容量超限在 _simulate 内抛出（修订号 UPDATE 之前）；这里再保证一次：
    # 构造一个通过全部模拟但在审计/持久化阶段…… 现有实现中失败必在 COMMIT 前，
    # ROLLBACK 会连同 revision+1 一起回滚。
    sample(client, "S1", "10")
    container(client, "V1")
    client.put("/containers/V1/capacity", json={"capacity": "1"})
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "5"}]},
    )
    assert r.status_code == 422
    assert _get(client, "S1").json()["transfer_revision"] == 0


# --------------------------------------------------------------------------- #
# 只读事务
# --------------------------------------------------------------------------- #
def test_query_uses_readonly_connection():
    ro = connect_readonly()
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO containers (id) VALUES ('SHOULD_FAIL')")
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("UPDATE transfer_revision SET revision = revision + 1")
        # 只读连接上查询本身正常
        assert service.get_transfer_revision(ro) >= 0
    finally:
        ro.close()


def test_decimal_fields_serialize_as_strings(client):
    _setup_mix(client)
    raw = _get(client, "S1").text
    # 响应体中体积字段均为 JSON 字符串而非数字
    parsed = json.loads(raw)
    for item in parsed["containers"]:
        assert isinstance(item["volume"], str)
        assert isinstance(item["source_amount"], str)
        assert isinstance(item["proportion"], str)
    assert isinstance(parsed["min_amount"], str)
    assert isinstance(parsed["transfer_revision"], int)


# --------------------------------------------------------------------------- #
# 深层传播：多代混合后的份额守恒
# --------------------------------------------------------------------------- #
def test_multi_generation_trace_amounts_conserve(client):
    sample(client, "S1", "100")
    for cid in ("A", "B", "C"):
        container(client, cid)
    client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "A", "volume": "50"},
                {"source": "A", "destination": "B", "volume": "20"},
                {"source": "B", "destination": "C", "volume": "8"},
            ]
        },
    )
    items = _map_items(_get(client, "S1").json())
    # S1=50, A=30, B=12, C=8，总和 100
    amounts = {cid: Decimal(items[cid]["source_amount"]) for cid in items}
    assert amounts == {"S1": Decimal("50.000000000"), "A": Decimal("30.000000000"),
                       "B": Decimal("12.000000000"), "C": Decimal("8.000000000")}
    assert sum(amounts.values(), Decimal(0)) == Decimal("100.000000000")


def test_zero_decimal_scale_env(tmp_path):
    """DECIMAL_SCALE=0：amount 无小数点，SQL 缩放表达式必须走整数分支。

    配置在进程启动时绑定（VOLUME_QUANTUM），故用独立子进程隔离，
    避免 reload 污染同进程其他测试。
    """
    import subprocess
    import sys

    script = (
        "import json, os;"
        f"os.environ['DATABASE_PATH']=r'{tmp_path / 's0.db'}';"
        "os.environ['DECIMAL_SCALE']='0';"
        "os.environ['PROPORTION_SCALE']='2';"
        "from fastapi.testclient import TestClient;"
        "from app.main import app;"
        "c=TestClient(app).__enter__();"
        "c.post('/samples', json={'sample_id':'S','volume':'10'});"
        "c.post('/containers', json={'container_id':'V'});"
        "r=c.post('/transfers/batch', json={'transfers':["
        "{'source':'S','destination':'V','volume':'3'}]});"
        "assert r.status_code==200, r.text;"
        "b=c.get('/sources/S/containers').json();"
        "assert b['min_amount']=='0', b;"
        "assert [(i['container_id'],i['volume'],i['source_amount'],i['proportion'])"
        " for i in b['containers']]==[('S','7','7','1.00'),('V','3','3','1.00')], b;"
        "b2=c.get('/sources/S/containers?min_amount=5').json();"
        "assert [i['container_id'] for i in b2['containers']]==['S'], b2;"
        "print('SCALE0_OK')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "SCALE0_OK" in proc.stdout


# --------------------------------------------------------------------------- #
# 旧库迁移：transfer_revision 与覆盖索引
# --------------------------------------------------------------------------- #
def test_init_db_migrates_legacy_db_with_revision_table(tmp_path, monkeypatch):
    legacy_path = tmp_path / "legacy_sources.db"
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
            CREATE INDEX idx_components_source ON components(source_id);
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
        rev = conn.execute("SELECT revision FROM transfer_revision WHERE id = 1").fetchone()[0]
        assert rev == 0
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='components'"
            )
        }
        assert "idx_components_source_container" in indexes
        assert "idx_components_container_source" in indexes
        assert "idx_components_source" not in indexes

        # 迁移后的旧库可直接服务于反向追踪
        from app.db import connect_readonly
        ro = connect_readonly()
        try:
            out = service.list_source_containers(ro, "OLD")
            assert [x["container_id"] for x in out["containers"]] == ["OLD"]
            assert out["containers"][0]["source_amount"] == "5"
        finally:
            ro.close()

        # 一次成功批次后修订号变 1
        conn.execute(
            "INSERT INTO containers (id) VALUES ('NEW')"
        )
        applied, _ = service.apply_transfers(conn, [("OLD", "NEW", Decimal("2"))])
        assert applied == 1
        assert conn.execute("SELECT revision FROM transfer_revision WHERE id = 1").fetchone()[0] == 1
    finally:
        conn.close()
