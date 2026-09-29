"""样品损耗核销的只追加记录（append-only loss records）。

每次**成功核销**（:func:`app.service.record_container_loss`，与组件扣减、
``version`` 递增、``transfer_revision`` 递增在**同一个 SQLite 事务**内）
写入一条损耗记录：

* ``loss_records``：核销容器、核销后实测体积、总损耗（核销前体积 − 实测体积）、
  核销前后的容器 ``version``、可选备注与追加时间；
* ``loss_record_sources``：该次核销按**核销前各祖先份额比例**（与转移相同的
  定点精度 + 最大余额法）从各来源实际扣减的份额明细，逐行 ``amount`` 之和
  严格等于 ``loss_records.loss_volume``。

只追加
------
两表各有一对 BEFORE UPDATE / BEFORE DELETE 触发器，任何应用内连接都无法
修改或删除既有记录（触发器在 :func:`app.db.init_db` 启动时幂等安装）。
损耗记录只允许 INSERT；失败的核销（版本冲突、实测值越界等，事务回滚）
**不产生**记录。
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal

from .service import LineageError

# --------------------------------------------------------------------------- #
# 表结构（由 app.db.init_db 的 executescript 幂等执行）
# --------------------------------------------------------------------------- #
LOSS_SCHEMA_SQL = """
-- 每次成功核销一条损耗记录。只追加：应用侧不允许 UPDATE / DELETE
-- （由 loss_records_no_update / loss_records_no_delete 触发器强制）。
CREATE TABLE IF NOT EXISTS loss_records (
    -- 全局单调递增序号（INTEGER PRIMARY KEY 即 rowid，回滚不留空洞）
    id              INTEGER PRIMARY KEY,
    container_id    TEXT NOT NULL,
    -- 核销后实测体积与总损耗（核销前体积 - 实测体积），Decimal 定点字符串
    measured_volume TEXT NOT NULL,
    loss_volume     TEXT NOT NULL,
    -- 核销前后的容器乐观版本（恰好 +1）
    version_before  INTEGER NOT NULL,
    version_after   INTEGER NOT NULL,
    -- 可选备注（未提供为 NULL）
    note            TEXT,
    created         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 损耗在各祖先来源之间的扣减明细：逐行 amount 之和严格等于总损耗。
-- 不设外键到 containers：损耗记录是独立的只追加事实，不随容器生命周期变化。
CREATE TABLE IF NOT EXISTS loss_record_sources (
    loss_id   INTEGER NOT NULL REFERENCES loss_records(id),
    source_id TEXT NOT NULL,
    amount    TEXT NOT NULL,
    -- 来源在该记录中的稳定排序位置（按 source_id 字典序）
    position  INTEGER NOT NULL,
    PRIMARY KEY (loss_id, source_id)
);

-- 按容器反查损耗记录（GET /containers/{id}/losses 按 id 倒序分页扫描）
CREATE INDEX IF NOT EXISTS idx_loss_records_container
    ON loss_records(container_id, id);
"""

# 只追加触发器：BEFORE UPDATE / BEFORE DELETE 一律 RAISE。
# 名称固定，安装前先查 sqlite_master，保证在任意 SQLite 版本上都幂等。
_TRIGGER_DEFS: dict[str, tuple[str, str, str]] = {
    "loss_records_no_update": (
        "loss_records",
        "UPDATE",
        "loss_records 是只追加损耗记录表：禁止 UPDATE（如需纠错请追加新记录）",
    ),
    "loss_records_no_delete": (
        "loss_records",
        "DELETE",
        "loss_records 是只追加损耗记录表：禁止 DELETE（如需纠错请追加新记录）",
    ),
    "loss_record_sources_no_update": (
        "loss_record_sources",
        "UPDATE",
        "loss_record_sources 是只追加损耗明细：禁止 UPDATE",
    ),
    "loss_record_sources_no_delete": (
        "loss_record_sources",
        "DELETE",
        "loss_record_sources 是只追加损耗明细：禁止 DELETE",
    ),
}


def install_loss_triggers(conn: sqlite3.Connection) -> None:
    """幂等安装四个只追加触发器（已存在则跳过），启动建库时调用。"""
    existing = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }
    for name, (table, event, message) in _TRIGGER_DEFS.items():
        if name in existing:
            continue
        conn.execute(
            f"CREATE TRIGGER {name} BEFORE {event} ON {table} "
            f"BEGIN SELECT RAISE(FAIL, '{message}'); END"
        )


# --------------------------------------------------------------------------- #
# 写入（仅在成功核销的写事务内调用）
# --------------------------------------------------------------------------- #
def append_loss_record(
    conn: sqlite3.Connection,
    *,
    container_id: str,
    measured_volume: Decimal,
    loss_volume: Decimal,
    deductions: dict[str, Decimal],
    version_before: int,
    version_after: int,
    note: str | None,
) -> int:
    """在当前（已开启的）写事务内追加一条损耗记录及其来源扣减明细。

    必须由 :func:`app.service.record_container_loss` 在组件扣减 /
    ``version`` 递增 / ``transfer_revision`` 递增之后、COMMIT 之前调用：
    任何后续失败都会连同损耗记录一起回滚，保证“失败不留痕”。
    返回新记录的 ``loss_id``。
    """
    cursor = conn.execute(
        "INSERT INTO loss_records"
        " (container_id, measured_volume, loss_volume,"
        "  version_before, version_after, note)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            container_id,
            str(measured_volume),
            str(loss_volume),
            version_before,
            version_after,
            note,
        ),
    )
    loss_id = int(cursor.lastrowid)
    conn.executemany(
        "INSERT INTO loss_record_sources (loss_id, source_id, amount, position)"
        " VALUES (?, ?, ?, ?)",
        [
            (loss_id, sid, str(amount), pos)
            for pos, (sid, amount) in enumerate(sorted(deductions.items()))
        ],
    )
    return loss_id


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def list_loss_records(conn: sqlite3.Connection, container_id: str) -> dict:
    """按**新到旧**返回某容器的全部损耗记录（含各来源扣减明细）。

    容器不存在返回 404 ``unknown_container``；容器存在但从未核销返回空列表。
    """
    exists = conn.execute(
        "SELECT 1 FROM containers WHERE id = ?", (container_id,)
    ).fetchone()
    if exists is None:
        raise LineageError(404, "unknown_container", f"容器 '{container_id}' 不存在")

    rows = conn.execute(
        "SELECT id, measured_volume, loss_volume, note, created"
        " FROM loss_records WHERE container_id = ? ORDER BY id DESC",
        (container_id,),
    ).fetchall()

    # 明细按 loss_id 一次取回并按 position 归组，避免逐记录点查
    loss_ids = [r["id"] for r in rows]
    deductions: dict[int, list[dict]] = {lid: [] for lid in loss_ids}
    if loss_ids:
        placeholders = ", ".join("?" for _ in loss_ids)
        for d in conn.execute(
            "SELECT loss_id, source_id, amount FROM loss_record_sources"
            f" WHERE loss_id IN ({placeholders}) ORDER BY loss_id, position",
            loss_ids,
        ):
            deductions[d["loss_id"]].append(
                {"source_id": d["source_id"], "amount": d["amount"]}
            )

    losses = [
        {
            "loss_id": r["id"],
            "measured_volume": r["measured_volume"],
            "loss_volume": r["loss_volume"],
            "deductions": deductions[r["id"]],
            "note": r["note"],
            "created": r["created"],
        }
        for r in rows
    ]
    return {"container_id": container_id, "count": len(losses), "losses": losses}


# --------------------------------------------------------------------------- #
# 测试辅助（生产代码不使用）
# --------------------------------------------------------------------------- #
def reset_for_tests(conn: sqlite3.Connection) -> None:
    """清空损耗记录表并恢复只追加触发器（仅测试使用）。

    由于 BEFORE DELETE 触发器禁止删除损耗行，测试夹具需要先临时摘除触发器、
    清空记录、再重新安装；生产路径永远不会调用本函数。
    """
    for name in _TRIGGER_DEFS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute("DELETE FROM loss_record_sources")
    conn.execute("DELETE FROM loss_records")
    install_loss_triggers(conn)
