"""PostgreSQL 连接与幂等初始化。"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

import psycopg2

from . import config


@contextmanager
def pg_conn(autocommit: bool = True) -> Iterator[psycopg2.extensions.connection]:
    """新建一个 PG 连接。演示站点并发很低，直接用短连接，避免连接池里残留事务状态。"""
    conn = psycopg2.connect(config.DATABASE_URL, connect_timeout=5, application_name="cal-recovery-web")
    conn.autocommit = autocommit
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def wait_for_postgres(max_wait_seconds: int = 60) -> None:
    deadline = time.time() + max_wait_seconds
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            return
        except Exception as exc:  # 数据库容器尚未就绪
            last_err = exc
            time.sleep(1)
    raise RuntimeError(f"等待 PostgreSQL 就绪超时: {last_err}")


def init_postgres() -> None:
    sql = config.SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    # pg_conn 默认 autocommit=True，可直接执行含多条 DDL/INSERT 的脚本。
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
