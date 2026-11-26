"""pytest 全局配置：在导入 app 之前固定到本地测试 PG 与临时 SQLite。

本地无 Docker 时，用 pgserver 解压出的 PostgreSQL（端口 55432）试跑；
CI/容器中可通过 TEST_DATABASE_URL 覆盖。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

_TEST_DIR = Path("/tmp/cal-recovery-test")
shutil.rmtree(_TEST_DIR, ignore_errors=True)
_TEST_DIR.mkdir(parents=True, exist_ok=True)

os.environ.setdefault(
    "TEST_DATABASE_URL",
    "host=/tmp port=55432 user=recovery dbname=caltest",
)
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["COORD_SQLITE_PATH"] = str(_TEST_DIR / "coord.db")
os.environ["WEB_SQLITE_PATH"] = str(_TEST_DIR / "web.db")
os.environ["ENABLE_CRASH_DEMO"] = "false"

import psycopg2  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, sqlite_store  # noqa: E402
from app.main import app  # noqa: E402
from app.pgdb import pg_conn  # noqa: E402


def _ensure_database() -> None:
    """caltest 库不存在则连维护库创建。"""
    base = os.environ["TEST_DATABASE_URL"]
    try:
        conn = psycopg2.connect(base)
        conn.close()
        return
    except psycopg2.OperationalError:
        pass
    admin_dsn = base.replace("dbname=caltest", "dbname=postgres")
    conn = psycopg2.connect(admin_dsn)
    conn.autocommit = True
    conn.cursor().execute("CREATE DATABASE caltest")
    conn.close()


def _reset_pg() -> None:
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT gid FROM pg_prepared_xacts WHERE gid LIKE 'GX-%'")
            gids = [r[0] for r in cur.fetchall()]
            for gid in gids:
                cur.execute(f"ROLLBACK PREPARED '{gid}'")
            cur.execute(
                "TRUNCATE param_revision, txn_commit_marker, publish_order, calibration_param RESTART IDENTITY CASCADE"
            )
    # 重新播种样例参数
    from app.pgdb import init_postgres

    init_postgres()


@pytest.fixture(scope="session")
def client():
    _ensure_database()
    config.ensure_sqlite_dirs()
    sqlite_store.init_coord_db()
    sqlite_store.init_web_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def reset_each():
    """每个用例前清空业务表/登记簿，保证断言干净。"""
    _reset_pg()
    for p in (config.WEB_SQLITE_PATH, config.COORD_SQLITE_PATH):
        try:
            Path(p).unlink()
        except FileNotFoundError:
            pass
        for suffix in ("-wal", "-shm"):
            try:
                Path(p + suffix).unlink()
            except FileNotFoundError:
                pass
    sqlite_store.init_coord_db()
    sqlite_store.init_web_db()
    yield
    # 用例后清理本用例可能遗留的预备事务，避免锁累积
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT gid FROM pg_prepared_xacts WHERE gid LIKE 'GX-%'")
            for (gid,) in cur.fetchall():
                cur.execute(f"ROLLBACK PREPARED '{gid}'")
