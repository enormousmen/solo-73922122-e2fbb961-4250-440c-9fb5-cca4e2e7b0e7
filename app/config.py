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

# ------------------------------------------------------------------ 发布台名额
# 总名额（一席对应一把 PostgreSQL 事务级 advisory lock，并可能变成一个 PREPARE TRANSACTION）。
# 演示用 2：1 个普通席位 + 1 个只留给指定抢修设备的预留席位。
PUBLISH_MAX_SLOTS = int(os.getenv("PUBLISH_MAX_SLOTS", "2"))

# 指定的抢修设备（预留席只对这些设备开放）。支持逗号分隔多台。
# 兼容 EMERGENCY_DEVICE_NO 单数写法。
_DEFAULT_EMERGENCY_DEVICE = "DEV-EMG-01"
EMERGENCY_DEVICE_NOS = {
    x.strip()
    for x in os.getenv(
        "EMERGENCY_DEVICE_NOS",
        os.getenv("EMERGENCY_DEVICE_NO", _DEFAULT_EMERGENCY_DEVICE),
    ).split(",")
    if x.strip()
}

# 一笔发布等待参数行锁的最长时间（毫秒）。0 表示一直等到拿到锁/连接中断。
# 网页普通下单默认给 15s，避免误操作把 HTTP 请求永久挂住；一键演示的后台任务用 0。
PUBLISH_LOCK_TIMEOUT_MS = int(os.getenv("PUBLISH_LOCK_TIMEOUT_MS", "15000"))

# 行锁道具/后台任务的兜底自动释放时间（秒），防止演示后忘记解锁。
DEMO_LOCK_HOLD_SECONDS = int(os.getenv("DEMO_LOCK_HOLD_SECONDS", "600"))

GXID_RE = r"^[A-Za-z0-9_-]{3,190}$"


def ensure_sqlite_dirs() -> None:
    for p in (COORD_SQLITE_PATH, WEB_SQLITE_PATH):
        Path(p).expanduser().parent.mkdir(parents=True, exist_ok=True)
