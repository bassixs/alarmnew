"""Отправка карточки модерации администратору + алерты об ошибках."""
from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from aialarm.config import get_settings
from aialarm.logging import get_logger
from aialarm.moderation.service import get_pending, get_preview
from aialarm.moderation.districts import get_district_pending, get_district_preview

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
    visual = f"🚫 ВИЗУАЛ БРАТЬ НЕЛЬЗЯ: {p['visual_warning']}\n" if p.get("visual_warning") else ""
    source = f"🔗 источник: {p['source_url']}\n" if p.get("source_url") else ""
    visual_choice = _visual_choice_text(p)
    if p.get("is_manual"):
        # Собственный текст редактора не проходил новостный фильтр — оценка 0/100
        # и пустой тезис здесь только путают модератора.
        meta = f"✍️ СВОЙ ПОСТ\n{visual_choice}{'─' * 20}\n"
    else:
        meta = (
            f"{flag}{visual}📊 Насколько новость подходит каналу: {p['confidence']} из 100\n"
            f"📌 Тезис: {p['matched_thesis']}\n"
            f"{visual_choice}"
            f"{source}"
            f"{'─' * 20}\n"
        )
    return (meta + p["post_text"])[:_CARD_LIMIT]


def _card_buttons(post_id: int, p: dict) -> list:
    """Пока редактор не выбрал визуал, публиковать пост нельзя.

    Это делает выбор картинки первым шагом: исходное фото уже видно в карточке,
    а сгенерированное появится сразу после нажатия «Генерация».
    """
    from aialarm.moderation import max_client

    if p.get("media_mode") == "unselected":
        return max_client.visual_choice_buttons(post_id, p)
    return max_client.callback_buttons(post_id)


def _visual_choice_text(p: dict) -> str:
    labels = {
        "original": "оригинал",
        "generate": "генерация",
        "none": "без картинки",
    }
    mode = p.get("media_mode", "")
    recommendation = labels.get(p.get("visual_recommendation", ""), "выберите вручную")
    selected = labels.get(mode, "не выбран")
    text = f"🖼 Визуал: {selected}; агент рекомендует — {recommendation}\n"
    if p.get("visual_reason"):
        text += f"🤖 {p['visual_reason']}\n"
    return text


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
    max_client.send_message(
        chat,
        _card_text(p),
        buttons=_card_buttons(post_id, p),
        image_refs=p.get("image_urls"),
    )


def edit_card(post_id: int, message_id: str) -> bool:
    """Превратить карточку-оригинал в готовый пост без нового сообщения в MAX."""
    if get_settings().project.moderation.platform != "max":
        return False
    from aialarm.moderation import max_client

    p = get_pending(post_id)
    if not p:
        return False
    return max_client.edit_message(
        message_id,
        _card_text(p),
        buttons=_card_buttons(post_id, p),
        image_refs=p.get("image_urls"),
    )


def send_card(post_id: int) -> None:
    """Готовый (переписанный) пост -> карточка ✅/✏️/❌ на площадку из config."""
    if get_settings().project.moderation.platform == "max":
        _send_max(post_id)
    else:
        send_moderation_card_sync(post_id)


def finalize_card(post_id: int, message_id: str, header: str) -> bool:
    """Отредактировать карточку в финальное состояние: статус-заголовок + текст поста,
    БЕЗ кнопок (действие завершено, повторно нажать нельзя). Возвращает успех."""
    if get_settings().project.moderation.platform != "max" or not message_id:
        log.info("finalize_card_skip", post_id=post_id, has_mid=bool(message_id))
        return False
    from aialarm.moderation import max_client

    p = get_pending(post_id)
    if not p:
        log.info("finalize_card_skip", post_id=post_id, reason="no_pending")
        return False
    text = f"{header}\n{'─' * 20}\n{p['post_text']}"
    ok = max_client.edit_message(message_id, text[:_CARD_LIMIT], buttons=None)
    log.info("finalize_card", post_id=post_id, mid=str(message_id)[:24], ok=ok)
    return ok


# ── Карточка-оригинал (шаг 1): «Переписать» / «Отменить» ─────────────────────
def _preview_text(p: dict) -> str:
    sep = "─" * 20
    head = ["❗ ОРИГИНАЛ (не переписан)"]
    if p["is_sensitive"]:
        head.append("⚠️ ЧУВСТВИТЕЛЬНАЯ ТЕМА")
    if p.get("visual_warning"):
        head.append(f"🚫 ВИЗУАЛ БРАТЬ НЕЛЬЗЯ: {p['visual_warning']}")
    if p["has_image"]:
        head.append("🖼 есть фото")
    # body уже содержит заголовок первой строкой — title отдельно НЕ добавляем (иначе дубль).
    meta = "\n".join(head) + f"\n{sep}\n🔗 источник: {p['source_url']}\n{sep}\n\n"
    return (meta + p["body"])[:_CARD_LIMIT]


def _preview_keyboard_tg(raw_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✍️ Переписать", callback_data=f"pre:rewrite:{raw_id}"),
                InlineKeyboardButton(text="🗑 Отменить", callback_data=f"pre:cancel:{raw_id}"),
            ]
        ]
    )


async def _send_preview_tg(raw_id: int) -> None:
    s = get_settings()
    token, chat_id = s.secrets.telegram_bot_token, s.project.moderation.admin_chat_id
    if not token or not chat_id:
        log.warning("preview_notify_skip", reason="нет токена или admin_chat_id")
        return
    p = get_preview(raw_id)
    if not p:
        return
    bot = Bot(token)
    try:
        for _ in range(4):
            try:
                await bot.send_message(chat_id, _preview_text(p),
                                       reply_markup=_preview_keyboard_tg(raw_id), parse_mode=None)
                return
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
        await bot.send_message(chat_id, _preview_text(p),
                               reply_markup=_preview_keyboard_tg(raw_id), parse_mode=None)
    finally:
        await bot.session.close()


def _send_preview_max(raw_id: int) -> None:
    from aialarm.moderation import max_client

    chat = get_settings().project.moderation.max_chat_id
    if not chat:
        log.warning("preview_notify_skip", reason="нет max_chat_id")
        return
    p = get_preview(raw_id)
    if not p:
        return
    max_client.send_message(
        chat,
        _preview_text(p),
        buttons=max_client.preview_buttons(raw_id),
        image_refs=p.get("image_urls"),
    )


def send_preview(raw_id: int) -> None:
    """Отправить карточку-оригинал на площадку из config.moderation.platform."""
    if get_settings().project.moderation.platform == "max":
        _send_preview_max(raw_id)
    else:
        asyncio.run(_send_preview_tg(raw_id))


# ── Районные карточки в отдельном MAX-чате ───────────────────────────────────
def _district_prefix(p: dict, original: bool = False) -> str:
    state = "❗ ОРИГИНАЛ (не переписан)" if original else "🏘 ГОТОВЫЙ ПОСТ"
    visual = f"🚫 ВИЗУАЛ БРАТЬ НЕЛЬЗЯ: {p['visual_warning']}\n" if p.get("visual_warning") else ""
    return f"{state}\n📍 РАЙОН: {p['district_title']}\n{visual}{'─' * 20}\n"


def _district_preview_text(p: dict) -> str:
    return (_district_prefix(p, original=True) + f"🔗 источник: {p['source_url']}\n{'─' * 20}\n\n" + p["body"])[
        :_CARD_LIMIT
    ]


def _district_card_text(p: dict) -> str:
    meta = (
        _district_prefix(p)
        + f"📊 Подходит каналу: {p['confidence']} из 100\n"
        + f"🔗 источник: {p['source_url']}\n{'─' * 20}\n"
    )
    return (meta + p["post_text"])[ :_CARD_LIMIT]


def send_district_preview(post_id: int) -> None:
    from aialarm.moderation import max_client

    chat = get_settings().project.districts.moderation_max_chat_id
    p = get_district_preview(post_id)
    if not chat or not p:
        log.warning("district_preview_notify_skip", post_id=post_id)
        return
    max_client.send_message(
        chat, _district_preview_text(p), buttons=max_client.district_preview_buttons(post_id),
        image_refs=(p.get("image_urls") or [])[:1],
    )


def edit_district_card(post_id: int, message_id: str, notice: str = "") -> bool:
    from aialarm.moderation import max_client

    p = get_district_pending(post_id)
    if not p or not message_id:
        return False
    text = _district_card_text(p)
    if notice:
        text = f"{notice}\n{'─' * 20}\n{text}"
    return max_client.edit_message(
        message_id, text, buttons=max_client.district_callback_buttons(post_id),
        image_refs=(p.get("image_urls") or [])[:1],
    )


def edit_visual_choices(post_id: int, message_id: str) -> bool:
    """Открыть на готовой карточке выбор оригинала/генерации/отказа."""
    if get_settings().project.moderation.platform != "max" or not message_id:
        return False
    from aialarm.moderation import max_client

    p = get_pending(post_id)
    if not p:
        return False
    return max_client.edit_message(
        message_id,
        _card_text(p),
        buttons=max_client.visual_choice_buttons(post_id, p),
        image_refs=p.get("image_urls"),
    )


def finalize_district_card(post_id: int, message_id: str, header: str) -> bool:
    from aialarm.moderation import max_client

    p = get_district_pending(post_id)
    if not p or not message_id:
        return False
    text = f"{header}\n📍 РАЙОН: {p['district_title']}\n{'─' * 20}\n{p['post_text'] or p['body']}"
    return max_client.edit_message(message_id, text[:_CARD_LIMIT], buttons=None)


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
