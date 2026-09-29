"""容器对祖先初始样品的**最低留存份额**锁定（retention locks）。

可为任一容器登记针对某个祖先初始样品（必须已由 ``POST /samples`` 登记）的
最低留存体积 ``min_volume``：

* 批量转移提交（``POST /transfers/batch``）与预演（``POST /transfers/preview``）
  在顺序模拟的**每一步按既有定点分摊之后**，检查**源容器**各留存规则——
  该来源在源容器中分摊后的剩余份额一旦低于下限，整批拒绝
  （422 ``retention_breach``），即使同批后续步骤会把份额转回也一样；
* 样品损耗核销（``POST /containers/{id}/losses``）按实际来源扣减后执行
  **同一条**约束：任一被锁定来源核销后的份额低于下限即整体回滚。

只约束“从容器取走 / 核销”的场景；转入只会增加份额，不触发检查。

规则本身可经 ``PUT /containers/{id}/retention`` 设置 / 修改 / 解除
（``min_volume`` 传 ``null`` 表示解除）。每次**实际变更**使该容器的
``version`` 加 1（幂等重写同一值、解除一条不存在的规则都不是实际变更，
版本不变），但**不**递增全局 ``transfer_revision``（来源反查修订号），
也不产生审计记录。与容量 / 不相容规则一样，旧库启动时幂等建表，
初始为空（不限制任何来源）。
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal

from .config import VOLUME_QUANTUM

# --------------------------------------------------------------------------- #
# 表结构（由 app.db.init_db 的 executescript 幂等执行）
# --------------------------------------------------------------------------- #
RETENTION_SCHEMA_SQL = """
-- 容器对祖先初始样品的最低留存份额：(container_id, source_id) 为主键，
-- min_volume 为 Decimal 定点字符串（始终为正，且 <= 该来源当前份额）。
-- 与容量 / 不相容规则一样可随时修改或解除（普通可变表，非只追加）。
-- 不设从 source_id 到 containers(is_sample) 的外键约束：存在性与
-- “已登记为初始样品”的语义由 service 层在写入事务内校验。
CREATE TABLE IF NOT EXISTS retention_locks (
    container_id TEXT NOT NULL REFERENCES containers(id) ON DELETE CASCADE,
    source_id    TEXT NOT NULL,
    min_volume   TEXT NOT NULL,
    PRIMARY KEY (container_id, source_id)
);

-- 按容器列出 / 模拟时取该容器全部留存规则的覆盖索引
CREATE INDEX IF NOT EXISTS idx_retention_locks_container
    ON retention_locks(container_id, source_id, min_volume);
"""

ZERO = Decimal(0).quantize(VOLUME_QUANTUM)


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def load_all_locks(conn: sqlite3.Connection) -> dict[str, dict[str, Decimal]]:
    """读取全部留存规则，返回 ``{container_id: {source_id: min_volume}}``。

    批量模拟在事务内一次读入：与状态 / 版本 / 禁配规则读取处于同一快照，
    提交与预演因此拿到完全一致的约束集合。
    """
    locks: dict[str, dict[str, Decimal]] = {}
    for r in conn.execute(
        "SELECT container_id, source_id, min_volume FROM retention_locks"
    ):
        locks.setdefault(r["container_id"], {})[r["source_id"]] = Decimal(
            r["min_volume"]
        )
    return locks


def list_locks(conn: sqlite3.Connection, cid: str) -> list[tuple[str, Decimal]]:
    """读取某容器的全部留存规则，按 source_id 字典序返回。"""
    return [
        (r["source_id"], Decimal(r["min_volume"]))
        for r in conn.execute(
            "SELECT source_id, min_volume FROM retention_locks"
            " WHERE container_id = ? ORDER BY source_id",
            (cid,),
        )
    ]


# --------------------------------------------------------------------------- #
# 写入（仅在 set_container_retention 的写事务内调用）
# --------------------------------------------------------------------------- #
def get_lock(
    conn: sqlite3.Connection, cid: str, source_id: str
) -> Decimal | None:
    """读取某容器对某来源的当前下限；未登记规则时返回 ``None``。"""
    row = conn.execute(
        "SELECT min_volume FROM retention_locks"
        " WHERE container_id = ? AND source_id = ?",
        (cid, source_id),
    ).fetchone()
    return Decimal(row["min_volume"]) if row is not None else None


def upsert_lock(
    conn: sqlite3.Connection, cid: str, source_id: str, min_volume: Decimal
) -> None:
    """设置 / 修改某容器对某来源的最低留存份额（同事务内调用）。"""
    conn.execute(
        "INSERT INTO retention_locks (container_id, source_id, min_volume)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT(container_id, source_id) DO UPDATE SET min_volume = excluded.min_volume",
        (cid, source_id, format(min_volume, "f")),
    )


def delete_lock(conn: sqlite3.Connection, cid: str, source_id: str) -> None:
    """解除某容器对某来源的最低留存份额（规则不存在时为无操作，同事务内调用）。"""
    conn.execute(
        "DELETE FROM retention_locks WHERE container_id = ? AND source_id = ?",
        (cid, source_id),
    )


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def format_retention(
    conn: sqlite3.Connection, cid: str, version: int, shares: dict[str, Decimal]
) -> dict:
    """组装 PUT/GET 留存规则的响应体：当前规则（含各来源当前份额）与版本。

    ``shares`` 为调用方在写事务内读到的容器**提交后**状态
    （``{source_id: amount}``），保证回显的 current_share 与本次写入同一快照。
    """
    rules = [
        {
            "source_id": sid,
            "min_volume": format(minimum, "f"),
            "current_share": format(shares.get(sid, ZERO), "f"),
        }
        for sid, minimum in list_locks(conn, cid)
    ]
    return {"container_id": cid, "version": version, "retention": rules}
