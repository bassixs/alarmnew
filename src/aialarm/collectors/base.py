"""Базовые типы сбора и фабрика коллекторов по типу источника."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from aialarm.config import SourceCfg


@dataclass(slots=True)
class CollectedItem:
    source_type: str
    source_url: str
    region: str
    title: str
    body: str = ""
    image_url: str | None = None
    published_at: datetime | None = None
    # Ссылка на конкретную новость (для дедуп-ключа), если отличается от source_url фида.
    item_url: str = ""
    extra: dict = field(default_factory=dict)

    def dedup_key(self) -> str:
        return make_dedup_key(self.item_url or self.source_url, self.title)


def make_dedup_key(url: str, title: str) -> str:
    basis = f"{(url or '').strip().lower()}|{(title or '').strip().lower()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class Collector(Protocol):
    async def collect(self) -> list[CollectedItem]: ...


def build_collector(cfg: SourceCfg) -> Collector:
    """Фабрика: тип источника -> реализация коллектора."""
    # Ленивые импорты, чтобы, например, отсутствие Telethon не ломало RSS-путь.
    if cfg.type in ("rss", "aggregator"):
        from aialarm.collectors.rss import RssCollector

        return RssCollector(cfg)
    if cfg.type == "tg_web":
        from aialarm.collectors.tg_web import TgWebCollector

        return TgWebCollector(cfg)
    if cfg.type == "telegram":
        from aialarm.collectors.telegram_source import TelegramCollector

        return TelegramCollector(cfg)
    if cfg.type == "scrape":
        from aialarm.collectors.scraper import ScrapeCollector

        return ScrapeCollector(cfg)
    raise ValueError(f"Неизвестный тип источника: {cfg.type}")
