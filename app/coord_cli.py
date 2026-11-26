"""协调端命令行：向 SQLite 决定日志写入提交/撤销决定。

用法（容器内）：
  docker compose exec web python -m app.coord_cli list
  docker compose exec web python -m app.coord_cli decide GX-xxxx COMMIT --reason "校验通过"
  docker compose exec web python -m app.coord_cli decide GX-xxxx ABORT  --reason "超差"

裸机：
  COORD_SQLITE_PATH=/tmp/coord.db python -m app.coord_cli list
"""
from __future__ import annotations

import argparse
import sys

from . import config, sqlite_store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="2PC 协调端决定日志工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_decide = sub.add_parser("decide", help="写入 COMMIT/ABORT 决定（已存在决定时不覆盖）")
    p_decide.add_argument("gxid")
    p_decide.add_argument("decision", choices=["COMMIT", "ABORT"])
    p_decide.add_argument("--reason", default="")
    p_decide.add_argument("--by", default="coordinator-cli")

    sub.add_parser("list", help="列出全部协调决定")
    args = parser.parse_args(argv)

    config.ensure_sqlite_dirs()
    sqlite_store.init_coord_db()

    if args.cmd == "decide":
        row = sqlite_store.write_decision(args.gxid, args.decision, args.reason, args.by)
        print(f"决定已留存: {row['gxid']} -> {row['decision']} ({row['decided_by']} @ {row['created_at']})")
        print("现在可在恢复页按该决定收尾。")
        return 0

    for row in sqlite_store.list_decisions():
        print(f"{row['created_at']}  {row['gxid']}  {row['decision']:<6}  {row['decided_by']}  {row['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
