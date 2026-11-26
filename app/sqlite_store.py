"""SQLite 存储：

- ``coord.db``：协调端留下的提交/撤销决定（``coord_decision``），决定一旦写入不可更改。
- ``web.db``：恢复网页自己的办理登记簿（``recovery_record``）与审计事件流。

两个库文件可以分别放在 Docker 命名卷中；网页进程被杀死并重启后，
PostgreSQL 中的证据 + 这两个 SQLite 文件共同决定如何继续办理。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _db(path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = _connect(path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- 协调端决定

_COORD_SCHEMA = """
CREATE TABLE IF NOT EXISTS coord_decision (
    gxid       TEXT PRIMARY KEY,
    decision   TEXT NOT NULL CHECK (decision IN ('COMMIT','ABORT')),
    reason     TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL DEFAULT 'coordinator',
    created_at TEXT NOT NULL
);
"""


def init_coord_db() -> None:
    with _db(config.COORD_SQLITE_PATH) as conn:
        conn.execute(_COORD_SCHEMA)


def write_decision(gxid: str, decision: str, reason: str = "", decided_by: str = "coordinator") -> dict[str, Any]:
    """写入协调决定。决定不可变：已存在时原样返回旧决定。"""
    if decision not in ("COMMIT", "ABORT"):
        raise ValueError("decision 必须是 COMMIT 或 ABORT")
    with _db(config.COORD_SQLITE_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO coord_decision(gxid, decision, reason, decided_by, created_at)"
            " VALUES (?,?,?,?,?)",
            (gxid, decision, reason, decided_by, _now()),
        )
        row = conn.execute("SELECT * FROM coord_decision WHERE gxid=?", (gxid,)).fetchone()
        return dict(row)


def get_decision(gxid: str) -> dict[str, Any] | None:
    with _db(config.COORD_SQLITE_PATH) as conn:
        row = conn.execute("SELECT * FROM coord_decision WHERE gxid=?", (gxid,)).fetchone()
        return dict(row) if row else None


def list_decisions() -> list[dict[str, Any]]:
    with _db(config.COORD_SQLITE_PATH) as conn:
        rows = conn.execute("SELECT * FROM coord_decision ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- 网页登记簿

_WEB_SCHEMA = """
CREATE TABLE IF NOT EXISTS recovery_record (
    gxid       TEXT PRIMARY KEY,
    outcome    TEXT NOT NULL,   -- COMMITTED / ROLLED_BACK / CONFLICT / MANUAL
    handling   TEXT NOT NULL,   -- FINALIZED / RECONCILED / CONFLICT_ACK / MANUAL_VERIFY
    detail     TEXT NOT NULL DEFAULT '',
    operator   TEXT NOT NULL DEFAULT 'web',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recovery_event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    gxid       TEXT NOT NULL,
    event      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    operator   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recovery_event_gxid ON recovery_event(gxid);
"""


def init_web_db() -> None:
    with _db(config.WEB_SQLITE_PATH) as conn:
        conn.executescript(_WEB_SCHEMA)


def get_record(gxid: str) -> dict[str, Any] | None:
    with _db(config.WEB_SQLITE_PATH) as conn:
        row = conn.execute("SELECT * FROM recovery_record WHERE gxid=?", (gxid,)).fetchone()
        return dict(row) if row else None


@contextmanager
def immediate_web_tx() -> Iterator[sqlite3.Connection]:
    """网页登记簿的进程内/跨进程互斥临界区（SQLite 写事务，busy 时轮询等待）。

    同一 gxid 的并发收尾在 ``BEGIN IMMEDIATE`` 处串行化；
    ``recovery_record`` 主键是最终的幂等兜底。
    """
    conn = _connect(config.WEB_SQLITE_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def get_record_on_conn(conn: sqlite3.Connection, gxid: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM recovery_record WHERE gxid=?", (gxid,)).fetchone()
    return dict(row) if row else None


def insert_record_on_conn(
    conn: sqlite3.Connection,
    gxid: str,
    outcome: str,
    handling: str,
    detail: str,
    operator: str,
) -> None:
    now = _now()
    conn.execute(
        "INSERT INTO recovery_record(gxid, outcome, handling, detail, operator, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (gxid, outcome, handling, detail, operator, now, now),
    )


def list_records() -> list[dict[str, Any]]:
    with _db(config.WEB_SQLITE_PATH) as conn:
        rows = conn.execute("SELECT * FROM recovery_record ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]


def list_events(gxid: str) -> list[dict[str, Any]]:
    with _db(config.WEB_SQLITE_PATH) as conn:
        rows = conn.execute(
            "SELECT * FROM recovery_event WHERE gxid=? ORDER BY id ASC", (gxid,)
        ).fetchall()
        return [dict(r) for r in rows]


def append_event(conn: sqlite3.Connection, gxid: str, event: str, detail: str = "", operator: str = "") -> None:
    conn.execute(
        "INSERT INTO recovery_event(gxid, event, detail, operator, created_at) VALUES (?,?,?,?,?)",
        (gxid, event, detail, operator, _now()),
    )


def save_record_once(
    gxid: str,
    outcome: str,
    handling: str,
    detail: str = "",
    operator: str = "web",
    event: str = "RECORD_SAVED",
) -> tuple[str, dict[str, Any]]:
    """幂等登记：同一 gxid 只有第一次登记生效。

    返回 ``("created", record)`` 或 ``("exists", existing_record)``。
    """
    now = _now()
    with _db(config.WEB_SQLITE_PATH) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT * FROM recovery_record WHERE gxid=?", (gxid,)
            ).fetchone()
            if existing is not None:
                append_event(conn, gxid, "DUPLICATE_IGNORED", "重复办理被幂等忽略", operator)
                conn.commit()
                return "exists", dict(existing)

            conn.execute(
                "INSERT INTO recovery_record(gxid, outcome, handling, detail, operator, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (gxid, outcome, handling, detail, operator, now, now),
            )
            append_event(conn, gxid, event, detail, operator)
            conn.commit()
            row = conn.execute("SELECT * FROM recovery_record WHERE gxid=?", (gxid,)).fetchone()
            return "created", dict(row)
        except Exception:
            conn.rollback()
            raise


def add_event(gxid: str, event: str, detail: str = "", operator: str = "") -> None:
    with _db(config.WEB_SQLITE_PATH) as conn:
        append_event(conn, gxid, event, detail, operator)
