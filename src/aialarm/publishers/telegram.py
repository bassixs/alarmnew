"""Публикация в Telegram через Bot API (aiogram).

Бот — админ канала с правом постинга. Текст с кликабельным подвалом отправляем в
режиме HTML: тело поста экранируем, подвал добавляем готовым HTML (<a href>).
Лимиты Telegram: 4096 символов для sendMessage, 1024 для подписи к фото.
Фото берём локальным файлом (скачан при сборе) или, как запас, качаем по URL.
"""
from __future__ import annotations

import html
from pathlib import Path

import httpx
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError
from aiogram.types import BufferedInputFile

from aialarm.config import get_settings
from aialarm.logging import get_logger
from aialarm.publishers.base import Post, PublishResult
from aialarm.publishers.footer import render_footer

log = get_logger(__name__)

_TEXT_LIMIT = 4096
_CAPTION_LIMIT = 1024


async def _image_bytes(ref: str) -> bytes | None:
    """Байты картинки из локального файла или по URL (запасной путь)."""
    try:
        if not ref.startswith(("http://", "https://")):
            p = Path(ref)
            return p.read_bytes() if p.exists() else None
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(ref, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return r.content
    except Exception as e:  # noqa: BLE001
        log.warning("tg_image_load_failed", ref=ref[:80], error=str(e))
        return None


def _html_body(post: Post, limit: int) -> str:
    body = html.escape(post.rendered_text(limit))
    footer = render_footer("telegram", "html")
    return f"{body}\n\n{footer}" if footer else body


class TelegramPublisher:
    platform = "telegram"

    def __init__(self):
        s = get_settings()
        self._token = s.secrets.telegram_bot_token
        self._chat_id = s.project.channels.telegram

    async def publish(self, post: Post) -> PublishResult:
        if not self._token or not self._chat_id:
            return PublishResult(ok=False, error="TELEGRAM_BOT_TOKEN или channels.telegram не заданы")

        bot = Bot(self._token)
        try:
            img = await _image_bytes(post.image_url) if post.image_url else None
            # С фото caption ограничен 1024. Если пост длиннее — публикуем текстом (без потери).
            caption = _html_body(post, _CAPTION_LIMIT - 200)
            if img and len(caption) <= _CAPTION_LIMIT:
                photo = BufferedInputFile(img, filename="image.jpg")
                msg = await bot.send_photo(
                    self._chat_id, photo=photo, caption=caption, parse_mode=ParseMode.HTML
                )
            else:
                if img:
                    log.info("tg_photo_dropped_caption_too_long")
                text = _html_body(post, _TEXT_LIMIT - 300)
                msg = await bot.send_message(self._chat_id, text, parse_mode=ParseMode.HTML,
                                             disable_web_page_preview=True)
            return PublishResult(ok=True, external_id=str(msg.message_id))
        except TelegramRetryAfter as e:
            log.warning("tg_rate_limited", retry_after=e.retry_after)
            return PublishResult(ok=False, error=f"rate limited: retry after {e.retry_after}s",
                                 rate_limited=True)
        except TelegramAPIError as e:
            log.error("tg_publish_failed", error=str(e))
            return PublishResult(ok=False, error=str(e))
        finally:
            await bot.session.close()
