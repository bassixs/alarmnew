"""Скрапинг сайтов без RSS — запасной вариант, юридически более рискованный.

Уважаем robots.txt: перед скрапингом проверяем разрешение. Эвристический разбор
заголовка/текста через Open Graph и типовые контейнеры. Для конкретного СМИ обычно
нужны точечные CSS-селекторы — вынесены в SourceCfg.extra['selectors'] при желании.
"""
from __future__ import annotations

import urllib.robotparser as robotparser
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from aialarm.collectors.base import CollectedItem
from aialarm.config import SourceCfg
from aialarm.logging import get_logger

log = get_logger(__name__)
_UA = "aialarm/0.1 (+news-rewriter)"


async def _robots_allowed(url: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = robotparser.RobotFileParser()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(robots_url, headers={"User-Agent": _UA})
            if resp.status_code >= 400:
                return True  # нет robots.txt — считаем разрешённым
            rp.parse(resp.text.splitlines())
        return rp.can_fetch(_UA, url)
    except Exception:  # noqa: BLE001
        return True


class ScrapeCollector:
    def __init__(self, cfg: SourceCfg):
        self.cfg = cfg

    async def collect(self) -> list[CollectedItem]:
        if not await _robots_allowed(self.cfg.url):
            log.warning("scrape_disallowed_by_robots", url=self.cfg.url)
            return []
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(self.cfg.url, headers={"User-Agent": _UA})
                resp.raise_for_status()
                html = resp.text
        except Exception as e:  # noqa: BLE001
            log.warning("scrape_fetch_failed", url=self.cfg.url, error=str(e))
            return []

        soup = BeautifulSoup(html, "html.parser")
        title = self._meta(soup, "og:title") or (soup.title.string if soup.title else "") or ""
        title = title.strip()
        if not title:
            return []
        body = self._extract_body(soup)
        image = self._meta(soup, "og:image")
        if image:
            image = urljoin(self.cfg.url, image)
        return [
            CollectedItem(
                source_type="scrape",
                source_url=self.cfg.url,
                region=self.cfg.region,
                title=title,
                body=body,
                image_url=image,
                published_at=datetime.now(timezone.utc),
                item_url=self.cfg.url,
            )
        ]

    @staticmethod
    def _meta(soup: BeautifulSoup, prop: str) -> str | None:
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        return tag.get("content") if tag else None

    def _extract_body(self, soup: BeautifulSoup) -> str:
        selectors = self.cfg.extra.get("selectors", {}) if hasattr(self.cfg, "extra") else {}
        node = None
        if selectors.get("body"):
            node = soup.select_one(selectors["body"])
        if node is None:
            node = soup.find("article") or soup.find("main") or soup.body
        if node is None:
            return ""
        paras = [p.get_text(" ", strip=True) for p in node.find_all("p")]
        return "\n\n".join(p for p in paras if p)
