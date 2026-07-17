"""Отправка карточки модерации администратору + алерты об ошибках."""
from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from aialarm.config import get_settings
from aialarm.logging import get_logger
from aialarm.moderation.service import get_pending

log = get_logger(__name__)

_CARD_LIMIT = 3500


def _keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"mod:approve:{post_id}"),
                InlineKeyboardButton(text="✏️ Править", callback_data=f"mod:edit:{post_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mod:reject:{post_id}"),
            ]
        ]
    )


def _card_text(p: dict) -> str:
    flag = "⚠️ ЧУВСТВИТЕЛЬНАЯ ТЕМА\n" if p["is_sensitive"] else ""
    meta = (
        f"{flag}📊 confidence: {p['confidence']} | тезис: {p['matched_thesis']}\n"
        f"🔗 источник: {p['source_url']}\n"
        f"{'─' * 20}\n"
    )
    return (meta + p["post_text"])[:_CARD_LIMIT]


async def _send(post_id: int) -> None:
    s = get_settings()
    token = s.secrets.telegram_bot_token
    chat_id = s.project.moderation.admin_chat_id
    if not token or not chat_id:
        log.warning("moderation_notify_skip", reason="нет токена или admin_chat_id")
        return
    p = get_pending(post_id)
    if not p:
        return
    bot = Bot(token)
    try:
        # При флуд-контроле Telegram ждём указанное время и повторяем.
        for _ in range(4):
            try:
                await bot.send_message(
                    chat_id, _card_text(p), reply_markup=_keyboard(post_id), parse_mode=None
                )
                return
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
        # последняя попытка — пусть пробросит исключение наверх
        await bot.send_message(
            chat_id, _card_text(p), reply_markup=_keyboard(post_id), parse_mode=None
        )
    finally:
        await bot.session.close()


def send_moderation_card_sync(post_id: int) -> None:
    asyncio.run(_send(post_id))


def _send_max(post_id: int) -> None:
    from aialarm.moderation import max_client

    chat = get_settings().project.moderation.max_chat_id
    if not chat:
        log.warning("moderation_notify_skip", reason="нет max_chat_id")
        return
    p = get_pending(post_id)
    if not p:
        return
    max_client.send_message(chat, _card_text(p), buttons=max_client.callback_buttons(post_id))


def send_card(post_id: int) -> None:
    """Отправить карточку модерации на площадку из config.moderation.platform."""
    if get_settings().project.moderation.platform == "max":
        _send_max(post_id)
    else:
        send_moderation_card_sync(post_id)


async def _alert(text: str) -> None:
    s = get_settings()
    token = s.secrets.telegram_bot_token
    chat_id = s.project.moderation.admin_chat_id
    if not token or not chat_id:
        return
    bot = Bot(token)
    try:
        await bot.send_message(chat_id, f"🚨 aialarm: {text}"[:4000], parse_mode=None)
    finally:
        await bot.session.close()


def alert_admin(text: str) -> None:
    """Алерт администратору об ошибке (rate limit, потеря прав бота и т.п.)."""
    try:
        asyncio.run(_alert(text))
    except Exception as e:  # noqa: BLE001
        log.error("alert_failed", error=str(e))
