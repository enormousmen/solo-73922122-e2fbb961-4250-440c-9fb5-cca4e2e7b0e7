"""全局配置：全部可通过环境变量覆盖（见 docker-compose.yml / .env.example）。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SCHEMA_SQL_PATH = BASE_DIR / "db" / "01_schema.sql"

# Docker Compose 中通过 DATABASE_URL 指向 db 服务；
# 裸机试用可用 keyword/value 形式，例如 host=/tmp port=55432 user=recovery dbname=caldb
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://recovery:recovery_pw@localhost:5432/caldb",
)

# 协调端的决定日志（SQLite）。协调器与网页可共享同一个卷。
COORD_SQLITE_PATH = os.getenv("COORD_SQLITE_PATH", "data/coordinator.db")
# 恢复网页自己的办理登记簿（SQLite）。
WEB_SQLITE_PATH = os.getenv("WEB_SQLITE_PATH", "data/recovery.db")

# 允许在“收尾后、登记前”让进程立即退出，以演示 Compose 重启后的证据恢复。
ENABLE_CRASH_DEMO = os.getenv("ENABLE_CRASH_DEMO", "false").lower() in {"1", "true", "yes", "on"}

# 锁演示等待时间（毫秒）。正常 Docker/Linux 上由 PostgreSQL lock_timeout 保证按时返回。
LOCK_DEMO_TIMEOUT_MS = int(os.getenv("LOCK_DEMO_TIMEOUT_MS", "3000"))

GXID_RE = r"^[A-Za-z0-9_-]{3,190}$"


def ensure_sqlite_dirs() -> None:
    for p in (COORD_SQLITE_PATH, WEB_SQLITE_PATH):
        Path(p).expanduser().parent.mkdir(parents=True, exist_ok=True)
