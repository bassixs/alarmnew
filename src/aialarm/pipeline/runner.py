"""Оркестрация конвейера: сбор -> фильтрация -> рерайт -> модерация -> публикация.

Функции-стадии переиспользуют модульные `run_*_stage`. Здесь — только склейка и
удобные обёртки для планировщика/CLI. При переезде на Celery каждая стадия
становится отдельной задачей; сигнатуры сохраняются.
"""
from __future__ import annotations

import asyncio

from aialarm.collectors import build_collector, store_items
from aialarm.config import SourceCfg, get_settings
from aialarm.filtering import run_filter_stage
from aialarm.logging import get_logger
from aialarm.moderation.service import route_after_rewrite
from aialarm.publishers.service import run_publish_stage
from aialarm.rewrite import run_rewrite_stage

log = get_logger(__name__)


async def _collect_source(cfg: SourceCfg) -> int:
    try:
        collector = build_collector(cfg)
        items = await collector.collect()
        stats = store_items(items)
        return stats["inserted"]
    except Exception as e:  # noqa: BLE001
        log.error("collect_source_failed", url=cfg.url, error=str(e))
        return 0


async def run_collection(only_source_url: str | None = None) -> dict[str, int]:
    """Собрать со всех включённых источников (или одного конкретного)."""
    sources = get_settings().project.sources
    active = [
        s for s in sources
        if s.enabled and (only_source_url is None or s.url == only_source_url)
    ]
    results = await asyncio.gather(*[_collect_source(s) for s in active])
    total = {"sources": len(active), "inserted": sum(results)}
    log.info("collection_done", **total)
    return total


def run_collection_sync(only_source_url: str | None = None) -> dict[str, int]:
    return asyncio.run(run_collection(only_source_url))


def run_processing() -> dict[str, dict]:
    """Стадии после сбора: фильтр -> рерайт -> маршрутизация модерации -> публикация."""
    result = {
        "filter": run_filter_stage(),
        "rewrite": run_rewrite_stage(),
        "moderation": route_after_rewrite(),
        "publish": run_publish_stage(),
    }
    return result


def run_full_pipeline() -> dict:
    collect = run_collection_sync()
    processing = run_processing()
    return {"collect": collect, **processing}
