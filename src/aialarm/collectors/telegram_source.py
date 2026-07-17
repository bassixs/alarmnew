"""Чтение публичных Telegram-каналов через MTProto (Telethon).

Читать открытые публикации публичных каналов легально. Требуются api_id/api_hash
(https://my.telegram.org/apps) и один раз пройденная авторизация сессии (см. README).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aialarm.collectors.base import CollectedItem
from aialarm.config import SourceCfg, get_settings
from aialarm.logging import get_logger

log = get_logger(__name__)


class TelegramCollector:
    def __init__(self, cfg: SourceCfg, lookback_hours: int = 6):
        self.cfg = cfg
        self.lookback = timedelta(hours=lookback_hours)

    async def collect(self) -> list[CollectedItem]:
        from telethon import TelegramClient

        s = get_settings().secrets
        if not s.telegram_api_id or not s.telegram_api_hash:
            log.warning("telethon_not_configured", channel=self.cfg.url)
            return []

        channel = self.cfg.url.lstrip("@")
        since = datetime.now(timezone.utc) - self.lookback
        items: list[CollectedItem] = []
        client = TelegramClient(s.telethon_session, s.telegram_api_id, s.telegram_api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                log.error("telethon_unauthorized", hint="Запустите scripts/telethon_login.py")
                return []
            async for msg in client.iter_messages(channel, limit=50):
                if not msg.message:
                    continue
                if msg.date and msg.date < since:
                    break
                text = msg.message.strip()
                title = text.split("\n", 1)[0][:200]
                image_url = None  # медиа Telegram требует отдельной выгрузки файла — опускаем в пилоте
                items.append(
                    CollectedItem(
                        source_type="telegram",
                        source_url=f"https://t.me/{channel}",
                        region=self.cfg.region,
                        title=title,
                        body=text,
                        image_url=image_url,
                        published_at=msg.date,
                        item_url=f"https://t.me/{channel}/{msg.id}",
                    )
                )
        except Exception as e:  # noqa: BLE001
            log.warning("telethon_collect_failed", channel=channel, error=str(e))
        finally:
            await client.disconnect()
        log.info("telegram_collected", channel=channel, count=len(items))
        return items
