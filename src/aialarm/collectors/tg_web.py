"""Чтение публичных Telegram-каналов через веб-превью t.me/s/<канал>.

Без аккаунта, без MTProto, без API — обычный HTTP к открытой веб-версии канала.
Telegram сам отдаёт последние посты публичного канала в виде HTML. Юридически это
открытая публикация, как и RSS.

Trade-off vs Telethon: проще (нет логина и сессии), но отдаёт только последние ~15-20
постов и без части метаданных. Для оперативного мониторинга новостей этого достаточно.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from aialarm.collectors.base import CollectedItem
from aialarm.config import SourceCfg
from aialarm.logging import get_logger

log = get_logger(__name__)
_UA = "Mozilla/5.0 (compatible; aialarm/0.1)"
_BG_RE = re.compile(r"url\(['\"]?(.*?)['\"]?\)")


class TgWebCollector:
    def __init__(self, cfg: SourceCfg):
        self.cfg = cfg
        self.channel = cfg.url.lstrip("@").rstrip("/").split("/")[-1]

    async def collect(self) -> list[CollectedItem]:
        url = f"https://t.me/s/{self.channel}"
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": _UA})
                resp.raise_for_status()
                html = resp.text
        except Exception as e:  # noqa: BLE001
            log.warning("tgweb_fetch_failed", channel=self.channel, error=str(e))
            return []

        soup = BeautifulSoup(html, "html.parser")
        items: list[CollectedItem] = []
        for msg in soup.select("div.tgme_widget_message"):
            text = self._extract_text(msg)
            if not text:
                continue  # чисто медиа/сервисные сообщения пропускаем
            post_url = self._post_url(msg)
            items.append(
                CollectedItem(
                    source_type="tg_web",
                    source_url=f"https://t.me/{self.channel}",
                    region=self.cfg.region,
                    title=text.split("\n", 1)[0][:200],
                    body=text,
                    image_url=self._extract_image(msg),
                    published_at=self._extract_dt(msg),
                    item_url=post_url,
                )
            )
        log.info("tgweb_collected", channel=self.channel, count=len(items))
        return items

    @staticmethod
    def _extract_text(msg) -> str:
        node = msg.select_one("div.tgme_widget_message_text")
        if not node:
            return ""
        for br in node.find_all("br"):
            br.replace_with("\n")
        text = node.get_text("", strip=False)
        # схлопываем лишние пустые строки
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text

    def _post_url(self, msg) -> str:
        a = msg.select_one("a.tgme_widget_message_date")
        if a and a.get("href"):
            return a["href"]
        link = msg.get("data-post")
        return f"https://t.me/{link}" if link else f"https://t.me/{self.channel}"

    @staticmethod
    def _extract_dt(msg) -> datetime | None:
        t = msg.select_one("a.tgme_widget_message_date time")
        if t and t.get("datetime"):
            try:
                return datetime.fromisoformat(t["datetime"]).astimezone(timezone.utc)
            except ValueError:
                return None
        return None

    @staticmethod
    def _extract_image(msg) -> str | None:
        wrap = msg.select_one("a.tgme_widget_message_photo_wrap") or msg.select_one(
            "i.tgme_widget_message_photo"
        )
        if wrap and wrap.get("style"):
            m = _BG_RE.search(wrap["style"])
            if m:
                return m.group(1)
        return None
