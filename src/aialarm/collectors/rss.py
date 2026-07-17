"""RSS и агрегаторы (Google News RSS). Самый стабильный и юридически чистый источник.

Для агрегаторов текст полноценно не забираем (это лишь обнаружение инфоповода):
берём заголовок и ссылку, полный текст добирается на этапе скрапинга/рерайта из
первоисточника при необходимости.
"""
from __future__ import annotations

from datetime import datetime, timezone
from time import mktime

import feedparser
import httpx

from aialarm.collectors.base import CollectedItem
from aialarm.config import SourceCfg
from aialarm.logging import get_logger

log = get_logger(__name__)


def _parse_dt(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            return datetime.fromtimestamp(mktime(val), tz=timezone.utc)
    return None


def _extract_image(entry) -> str | None:
    if entry.get("media_content"):
        url = entry["media_content"][0].get("url")
        if url:
            return url
    if entry.get("media_thumbnail"):
        url = entry["media_thumbnail"][0].get("url")
        if url:
            return url
    for link in entry.get("links", []):
        if link.get("type", "").startswith("image/"):
            return link.get("href")
    return None


class RssCollector:
    def __init__(self, cfg: SourceCfg):
        self.cfg = cfg

    async def collect(self) -> list[CollectedItem]:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(self.cfg.url, headers={"User-Agent": "aialarm/0.1"})
                resp.raise_for_status()
                content = resp.content
        except Exception as e:  # noqa: BLE001
            log.warning("rss_fetch_failed", url=self.cfg.url, error=str(e))
            return []

        feed = feedparser.parse(content)
        items: list[CollectedItem] = []
        for entry in feed.entries:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            summary = (entry.get("summary") or entry.get("description") or "").strip()
            body = _strip_html(summary)
            items.append(
                CollectedItem(
                    source_type=self.cfg.type,
                    source_url=self.cfg.url,
                    region=self.cfg.region,
                    title=title,
                    body=body,
                    image_url=_extract_image(entry),
                    published_at=_parse_dt(entry),
                    item_url=entry.get("link", ""),
                )
            )
        log.info("rss_collected", url=self.cfg.url, count=len(items))
        return items


def _strip_html(html: str) -> str:
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
