"""SQLite 连接管理与建库。

容器启动时调用 init_db() 建库建表；应用通过 FastAPI 依赖拿到连接。
为支持事务回滚，连接使用显式 BEGIN/COMMIT/ROLLBACK（autocommit 隔离级别）。
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .audit import AUDIT_SCHEMA_SQL, install_audit_triggers
from .config import settings
from .loss import LOSS_SCHEMA_SQL, install_loss_triggers
from .retention import RETENTION_SCHEMA_SQL

_SCHEMA = """
CREATE TABLE IF NOT EXISTS containers (
    id      TEXT PRIMARY KEY,
    -- 乐观并发版本：创建时为 0，每次成功的批量转移触碰后加 1（同批多次触碰只加 1）
    version INTEGER NOT NULL DEFAULT 0,
    -- 容量上限：NULL 表示不限制；非 NULL 为 Decimal 字符串，必须 >= 当前体积
    capacity TEXT,
    -- 是否经 POST /samples 登记的初始样品：不相容规则只允许引用初始样品 ID
    is_sample INTEGER NOT NULL DEFAULT 0,
    created INTEGER NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 容器中来自每个祖先初始样品的“绝对体积份额”。
-- 初始样品登记后占 1 行（source_id = 自身）；空容器为 0 行。
-- Decimal 以字符串存储，杜绝浮点误差。
CREATE TABLE IF NOT EXISTS components (
    container_id TEXT NOT NULL REFERENCES containers(id) ON DELETE CASCADE,
    source_id    TEXT NOT NULL,
    amount       TEXT NOT NULL,
    PRIMARY KEY (container_id, source_id)
);

-- 反向追踪“哪些容器仍含某初始样品”的覆盖索引：按 source_id 一次范围
-- 扫描即可有序拿到 (container_id, amount)，无需回表（GET /sources/{id}/containers）。
CREATE INDEX IF NOT EXISTS idx_components_source_container
    ON components(source_id, container_id, amount);

-- 正向“容器体积 / 来源构成”查询的覆盖索引：按 container_id 点查 / SUM(amount)
-- 走仅索引扫描，无需回表（GET /containers/{id} 与反向追踪的体积聚合）。
CREATE INDEX IF NOT EXISTS idx_components_container_source
    ON components(container_id, source_id, amount);

-- 全局转移修订号（单行，id 恒为 1）：每个成功提交的批次在其写事务内
-- 原子 +1；预演、失败批次与幂等重放不递增。祖先来源反向追踪的分页游标
-- 携带查询时刻的修订号，翻页期间号变化即判定游标过期（409 stale_cursor）。
CREATE TABLE IF NOT EXISTS transfer_revision (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL DEFAULT 0
);

-- 旧库升级时补出唯一一行（修订号从 0 起，历史批次不追溯计数）
INSERT OR IGNORE INTO transfer_revision (id, revision) VALUES (1, 0);

-- POST /transfers/batch 的幂等记录：仅在批次成功提交时，与容器扣减/版本递增
-- 写入同一个事务。重放（同键同请求体）直接返回 response_body，不再扣减体积。
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    -- 首次成功请求的原始请求体指纹（SHA-256 hex）；同键但指纹不同 -> 409
    request_sha256  TEXT NOT NULL,
    -- 首次响应的 HTTP 状态码与完整响应体（compact JSON 原文，逐字节重放）
    status_code     INTEGER NOT NULL,
    response_body   TEXT NOT NULL,
    created INTEGER NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 祖先来源不相容规则（无向禁配对）：(source_a, source_b) 以字典序规范化存储
-- （source_a < source_b），主键天然去重。PUT /compatibility/rules 原子全量
-- 替换本表；批量提交与预演在顺序模拟的每一步分摊后，检查目标容器的非零
-- 祖先集合是否同时含有某一对的双方。
CREATE TABLE IF NOT EXISTS compatibility_rules (
    source_a TEXT NOT NULL,
    source_b TEXT NOT NULL,
    PRIMARY KEY (source_a, source_b),
    CHECK (source_a < source_b)
);

-- 样品损耗核销记录（只追加）：每次成功核销在组件扣减 / version+1 /
-- transfer_revision+1 所在的同一事务内追加 loss_records 一行 +
-- loss_record_sources 各来源扣减明细行；两表由 BEFORE UPDATE/DELETE
-- 触发器禁止修改与删除（见 app/loss.py）。
""" + AUDIT_SCHEMA_SQL + LOSS_SCHEMA_SQL + RETENTION_SCHEMA_SQL


def _migrate(conn: sqlite3.Connection) -> None:
    """对已有数据库做幂等升级（旧库没有 containers.version / capacity /
    is_sample 列）。

    ``idempotency_keys``、``compatibility_rules``、``transfer_revision``
    与审计链相关的表（``audit_records``、``audit_record_containers``）由
    建表脚本中的 ``CREATE TABLE IF NOT EXISTS`` 负责补齐，旧库升级时自动
    创建，无需手工迁移；只追加审计触发器在此幂等安装。损耗核销表
    （``loss_records``、``loss_record_sources``）同样由建表脚本补齐，
    其只追加触发器在此幂等安装。最低留存份额表（``retention_locks``）
    也由建表脚本幂等补齐，旧库初始为空（不限制任何来源）。

    旧库 ``components`` 上的单列索引 ``idx_components_source`` 由建表脚本
    换成两个覆盖索引（``idx_components_source_container`` 支撑按来源反向
    追踪、``idx_components_container_source`` 支撑按容器聚合体积，均为仅
    索引扫描），此处幂等摘除旧索引。
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(containers)")}
    if columns and "version" not in columns:
        # 旧库中的容器版本统一视为 0（DEFAULT 0 会回填到每一行）
        conn.execute(
            "ALTER TABLE containers ADD COLUMN version INTEGER NOT NULL DEFAULT 0"
        )
    if columns and "capacity" not in columns:
        # 旧库中的容器一律不限制容量（NULL）
        conn.execute("ALTER TABLE containers ADD COLUMN capacity TEXT")
    if columns and "is_sample" not in columns:
        # 旧库无法直接区分初始样品与空容器：以“当前仍持有自身来源份额”
        # （components 中存在 container_id = source_id 的自指行）回填标记；
        # 已完全转空的旧样品无法被识别，如需引用可在回填后人工核对。
        conn.execute(
            "ALTER TABLE containers ADD COLUMN is_sample INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            "UPDATE containers SET is_sample = 1 WHERE id IN ("
            "SELECT container_id FROM components WHERE container_id = source_id)"
        )
    # 审计表的只追加触发器（BEFORE UPDATE/DELETE -> RAISE），已存在则跳过
    install_audit_triggers(conn)
    # 损耗记录表的只追加触发器（BEFORE UPDATE/DELETE -> RAISE），已存在则跳过
    install_loss_triggers(conn)
    # 旧的单列索引已被建表脚本中的覆盖索引取代（执行一次后为无操作的幂等 DDL）
    conn.execute("DROP INDEX IF EXISTS idx_components_source")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.database_path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # 显式事务
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    """创建数据目录与全部表（幂等，容器启动时执行），并兼容升级旧库。"""
    directory = os.path.dirname(os.path.abspath(settings.database_path))
    os.makedirs(directory, exist_ok=True)
    conn = connect()
    try:
        # executescript 自带事务语义；各语句均为 IF NOT EXISTS，幂等
        conn.executescript(_SCHEMA)
        # 旧版本数据库缺少 version 列时补齐（ALTER TABLE 幂等由本函数判断）
        _migrate(conn)
    finally:
        conn.close()


@contextmanager
def get_conn_context() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def connect_readonly() -> sqlite3.Connection:
    """以**只读**模式打开一个 SQLite 连接（``mode=ro`` URI）。

    SQLite 层直接拒绝任何写操作（INSERT/UPDATE/DELETE/DDL 抛
    ``attempt to write a readonly database``）；配合显式
    ``BEGIN`` / ``ROLLBACK``，查询期间读取的是事务开始时刻的一致性快照，
    翻页过程中其他连接提交的批次不会影响本次读取。供只读查询依赖使用。

    要求库文件已由 :func:`init_db` 初始化（容器启动 / 测试夹具均保证）。
    WAL 模式下只读连接无需写共享内存（3.22+），目录只读也能正常查询。
    """
    uri = pathlib.Path(os.path.abspath(settings.database_path)).as_uri()
    conn = sqlite3.connect(
        f"{uri}?mode=ro",
        uri=True,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # 显式事务
    )
    conn.row_factory = sqlite3.Row
    # query_only 作为应用侧第二重只读保险（mode=ro 已在 SQLite 层拦截写入）
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def get_readonly_conn_context() -> Iterator[sqlite3.Connection]:
    conn = connect_readonly()
    try:
        yield conn
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI 请求依赖（读写连接）。"""
    with get_conn_context() as conn:
        yield conn


def get_db_readonly() -> Iterator[sqlite3.Connection]:
    """FastAPI 请求依赖（只读连接）。"""
    with get_readonly_conn_context() as conn:
        yield conn
