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
from aiogram.types import BufferedInputFile, InputMediaPhoto

from aialarm.config import channels_for_profile, get_settings
from aialarm.control import get_publish_profile
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

    def __init__(self, profile: str | None = None):
        s = get_settings()
        self._profile = profile or get_publish_profile()
        channels = channels_for_profile(self._profile)
        self._token = s.secrets.telegram_bot_token
        self._chat_id = channels.telegram

    async def publish(self, post: Post) -> PublishResult:
        if not self._token or not self._chat_id:
            return PublishResult(ok=False, error=f"Telegram не настроен для профиля {self._profile}")

        bot = Bot(self._token)
        try:
            images: list[bytes] = []
            for ref in post.image_refs()[:10]:
                image = await _image_bytes(ref)
                if image:
                    images.append(image)

            text = _html_body(post, _TEXT_LIMIT - 300)
            caption = text if len(text) <= _CAPTION_LIMIT else None
            if len(images) == 1:
                msg = await bot.send_photo(
                    self._chat_id,
                    photo=BufferedInputFile(images[0], filename="image-01.jpg"),
                    caption=caption,
                    parse_mode=ParseMode.HTML if caption else None,
                )
            elif len(images) > 1:
                media = [
                    InputMediaPhoto(
                        media=BufferedInputFile(image, filename=f"image-{index + 1:02d}.jpg"),
                        caption=caption if index == 0 else None,
                        parse_mode=ParseMode.HTML if index == 0 and caption else None,
                    )
                    for index, image in enumerate(images)
                ]
                messages = await bot.send_media_group(self._chat_id, media=media)
                msg = messages[0]
            else:
                msg = await bot.send_message(
                    self._chat_id,
                    text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )

            # Caption альбома ограничен 1024 символами. Длинный текст отправляем следом,
            # не обрезая его и не выбрасывая фотографии.
            if images and caption is None:
                msg = await bot.send_message(
                    self._chat_id,
                    text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
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
