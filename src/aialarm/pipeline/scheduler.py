"""Планировщик пилота на APScheduler (in-process, без Redis).

Джобы:
- по одному на источник, с его poll_interval_min — только сбор;
- processing каждые PROCESS_INTERVAL_MIN — фильтр/рерайт/маршрутизация модерации;
- publish каждые min_minutes_between_posts — публикация одобренных с учётом лимитов.

Прод: заменить на Celery beat (расписание) + воркеры (стадии), логика та же.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

from aialarm.config import get_settings
from aialarm.db import init_db
from aialarm.collectors.images import cleanup_old
from aialarm.filtering import run_filter_stage
from aialarm.logging import configure_logging, get_logger
from aialarm.moderation.service import route_previews
from aialarm.pipeline.runner import run_collection_sync
from aialarm.publishers.service import run_publish_stage

log = get_logger(__name__)

PROCESS_INTERVAL_MIN = 5


def _processing_job() -> None:
    run_filter_stage()
    route_previews()          # шлём оригиналы на модерацию; рерайт — по кнопке «Переписать»
    cleanup_old(days=2)       # чистим старые скачанные картинки


def build_scheduler() -> BlockingScheduler:
    proj = get_settings().project
    sched = BlockingScheduler(timezone="UTC")

    # Одна задача сбора со всех источников (внутри — параллельный fetch, последовательная
    # запись). Отдельные задачи на источник давали конкурентные записи и 'database is locked'.
    now = datetime.now(timezone.utc)
    intervals = [s.poll_interval_min for s in proj.sources if s.enabled]
    collect_interval = min(intervals) if intervals else 20
    # next_run_time -> первый запуск вскоре после старта (не ждём полный интервал).
    sched.add_job(run_collection_sync, "interval", minutes=max(1, collect_interval),
                  id="collect", max_instances=1, coalesce=True,
                  next_run_time=now + timedelta(seconds=5))

    sched.add_job(_processing_job, "interval", minutes=PROCESS_INTERVAL_MIN, id="processing",
                  max_instances=1, coalesce=True,
                  next_run_time=now + timedelta(seconds=45))
    sched.add_job(
        run_publish_stage,
        "interval",
        minutes=max(1, proj.publish.min_minutes_between_posts),
        id="publish",
        max_instances=1,
        coalesce=True,
    )
    return sched


def main() -> None:
    configure_logging()
    init_db()
    sched = build_scheduler()
    log.info("scheduler_start", jobs=[j.id for j in sched.get_jobs()])
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler_stop")
