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
from aialarm.moderation.service import route_previews
from aialarm.publishers.service import run_publish_stage

log = get_logger(__name__)


async def _fetch_source(cfg: SourceCfg):
    try:
        return await build_collector(cfg).collect()
    except Exception as e:  # noqa: BLE001
        log.error("collect_source_failed", url=cfg.url, error=str(e))
        return []


async def run_collection(only_source_url: str | None = None) -> dict[str, int]:
    """Собрать со всех включённых источников. Скачиваем каналы параллельно, но пишем в БД
    ПОСЛЕДОВАТЕЛЬНО — иначе параллельные записи в SQLite дают 'database is locked'."""
    sources = get_settings().project.sources
    active = [
        s for s in sources
        if s.enabled and (only_source_url is None or s.url == only_source_url)
    ]
    fetched = await asyncio.gather(*[_fetch_source(s) for s in active])
    inserted = 0
    for items in fetched:
        try:
            inserted += store_items(items)["inserted"]
        except Exception as e:  # noqa: BLE001
            log.error("store_items_failed", error=str(e))
    total = {"sources": len(active), "inserted": inserted}
    log.info("collection_done", **total)
    return total


def run_collection_sync(only_source_url: str | None = None) -> dict[str, int]:
    return asyncio.run(run_collection(only_source_url))


def run_processing() -> dict[str, dict]:
    """Стадии после сбора: фильтр -> карточки-оригиналы на модерацию -> публикация
    одобренных. Рерайт (Sonnet) — по кнопке «Переписать» в боте, не здесь."""
    result = {
        "filter": run_filter_stage(),
        "preview": route_previews(),
        "publish": run_publish_stage(),
    }
    return result


def run_full_pipeline() -> dict:
    collect = run_collection_sync()
    processing = run_processing()
    return {"collect": collect, **processing}
