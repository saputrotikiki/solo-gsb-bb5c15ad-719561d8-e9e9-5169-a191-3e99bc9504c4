import os
import tempfile
from pathlib import Path

# 必须在导入 app 之前设置
_tmp = Path(tempfile.mkdtemp())
os.environ.setdefault("DATABASE_PATH", str(_tmp / "test.db"))
os.environ.setdefault("DECIMAL_SCALE", "9")
os.environ.setdefault("PROPORTION_SCALE", "6")

import pytest
from fastapi.testclient import TestClient

from app import audit, loss, service
from app.db import connect, init_db
from app.main import app
from decimal import Decimal


@pytest.fixture(scope="session", autouse=True)
def _init():
    init_db()
    yield


@pytest.fixture(autouse=True)
def _clean_tables():
    conn = connect()
    try:
        # 审计表有只追加触发器（禁止 DELETE），需先临时摘除触发器再清空
        audit.reset_for_tests(conn)
        # 损耗记录表同样有只追加触发器，需先摘除再清空
        loss.reset_for_tests(conn)
        conn.execute("DELETE FROM components")
        conn.execute("DELETE FROM containers")
        conn.execute("DELETE FROM idempotency_keys")
        conn.execute("DELETE FROM compatibility_rules")
        conn.execute("DELETE FROM retention_locks")
        # 全局转移修订号恢复为 0（单行表：成功批次 +1，跨测试会累积）
        conn.execute("UPDATE transfer_revision SET revision = 0 WHERE id = 1")
        yield
    finally:
        conn.close()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c
