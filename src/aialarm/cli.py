"""Единая точка входа CLI.

Примеры:
  python -m aialarm.cli initdb
  python -m aialarm.cli collect            # разовый сбор со всех источников
  python -m aialarm.cli process            # фильтр -> рерайт -> модерация -> публикация
  python -m aialarm.cli run                # полный проход один раз
  python -m aialarm.cli scheduler          # запустить планировщик (пилот)
  python -m aialarm.cli bot                # запустить бота-модератора (polling)
  python -m aialarm.cli api                # запустить дашборд/health (uvicorn)
  python -m aialarm.cli report --days 7
"""
from __future__ import annotations

import argparse
import json

from aialarm.db import init_db
from aialarm.logging import configure_logging, get_logger

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    parser = argparse.ArgumentParser(prog="aialarm")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("initdb", help="создать таблицы")
    sub.add_parser("collect", help="разовый сбор новостей")
    sub.add_parser("process", help="фильтр -> рерайт -> модерация -> публикация")
    sub.add_parser("run", help="полный проход конвейера один раз")
    sub.add_parser("scheduler", help="запустить планировщик (пилот)")
    sub.add_parser("bot", help="запустить бота-модератора")
    p_api = sub.add_parser("api", help="запустить FastAPI дашборд")
    p_api.add_argument("--host", default="127.0.0.1")
    p_api.add_argument("--port", type=int, default=8000)
    p_report = sub.add_parser("report", help="метрики воронки")
    p_report.add_argument("--days", type=int, default=7)

    args = parser.parse_args(argv)

    if args.cmd == "initdb":
        init_db()
        log.info("db_initialized")
    elif args.cmd == "collect":
        from aialarm.pipeline.runner import run_collection_sync

        init_db()
        print(json.dumps(run_collection_sync(), ensure_ascii=False, indent=2))
    elif args.cmd == "process":
        from aialarm.pipeline.runner import run_processing

        init_db()
        print(json.dumps(run_processing(), ensure_ascii=False, indent=2))
    elif args.cmd == "run":
        from aialarm.pipeline.runner import run_full_pipeline

        init_db()
        print(json.dumps(run_full_pipeline(), ensure_ascii=False, indent=2))
    elif args.cmd == "scheduler":
        from aialarm.pipeline.scheduler import main as sched_main

        sched_main()
    elif args.cmd == "bot":
        from aialarm.config import get_settings

        init_db()
        if get_settings().project.moderation.platform == "max":
            from aialarm.moderation.max_bot import main as bot_main
        else:
            from aialarm.moderation.bot import main as bot_main
        bot_main()
    elif args.cmd == "api":
        import uvicorn

        uvicorn.run("aialarm.api:app", host=args.host, port=args.port)
    elif args.cmd == "report":
        init_db()
        print(json.dumps(funnel_safe(args.days), ensure_ascii=False, indent=2))


def funnel_safe(days: int) -> dict:
    from aialarm.reporting import funnel

    return funnel(days)


if __name__ == "__main__":
    main()
