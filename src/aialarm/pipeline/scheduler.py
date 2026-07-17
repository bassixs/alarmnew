"""Планировщик пилота на APScheduler (in-process, без Redis).

Джобы:
- по одному на источник, с его poll_interval_min — только сбор;
- processing каждые PROCESS_INTERVAL_MIN — фильтр/рерайт/маршрутизация модерации;
- publish каждые min_minutes_between_posts — публикация одобренных с учётом лимитов.

Прод: заменить на Celery beat (расписание) + воркеры (стадии), логика та же.
"""
from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler

from aialarm.config import get_settings
from aialarm.db import init_db
from aialarm.filtering import run_filter_stage
from aialarm.logging import configure_logging, get_logger
from aialarm.moderation.service import route_after_rewrite
from aialarm.pipeline.runner import run_collection_sync
from aialarm.publishers.service import run_publish_stage
from aialarm.rewrite import run_rewrite_stage

log = get_logger(__name__)

PROCESS_INTERVAL_MIN = 5


def _processing_job() -> None:
    run_filter_stage()
    run_rewrite_stage()
    route_after_rewrite()


def build_scheduler() -> BlockingScheduler:
    proj = get_settings().project
    sched = BlockingScheduler(timezone="UTC")

    for src in proj.sources:
        if not src.enabled:
            continue
        sched.add_job(
            run_collection_sync,
            "interval",
            minutes=max(1, src.poll_interval_min),
            kwargs={"only_source_url": src.url},
            id=f"collect::{src.url}",
            max_instances=1,
            coalesce=True,
        )

    sched.add_job(_processing_job, "interval", minutes=PROCESS_INTERVAL_MIN, id="processing",
                  max_instances=1, coalesce=True)
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
