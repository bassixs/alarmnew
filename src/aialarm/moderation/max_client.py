"""Низкоуровневые вызовы MAX Bot API для модерации.

MAX (форк TamTam Bot API): авторизация заголовком Authorization, chat_id — query-параметр,
inline-кнопки — attachments типа inline_keyboard с кнопками type=callback. Нажатие
приходит апдейтом message_callback; ответ на него — POST /answers?callback_id=...
Домен и заголовок берём из config.max_platform (могут мигрировать).
"""
from __future__ import annotations

import httpx

from aialarm.config import get_settings
from aialarm.logging import get_logger

log = get_logger(__name__)


def _conn() -> tuple[str, str, str]:
    s = get_settings()
    base = s.project.max_platform.base_url.rstrip("/")
    return base, s.secrets.max_bot_token, s.project.max_platform.auth_header


def send_message(chat_id: str, text: str, buttons: list | None = None) -> dict:
    base, token, auth = _conn()
    body: dict = {"text": text}
    if buttons:
        body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]
    r = httpx.post(
        f"{base}/messages",
        params={"chat_id": str(chat_id)},
        json=body,
        headers={auth: token, "Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json() if r.content else {}


def get_updates(marker: int | None = None, timeout: int = 30) -> dict:
    base, token, auth = _conn()
    params: dict = {"timeout": timeout, "limit": 100}
    if marker is not None:
        params["marker"] = marker
    r = httpx.get(
        f"{base}/updates", params=params, headers={auth: token}, timeout=timeout + 15
    )
    r.raise_for_status()
    return r.json()


def delete_message(mid: str) -> None:
    base, token, auth = _conn()
    try:
        r = httpx.delete(
            f"{base}/messages", params={"message_id": mid}, headers={auth: token}, timeout=20
        )
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning("max_delete_failed", error=str(e))


def answer_callback(callback_id: str, notification: str | None = None) -> None:
    base, token, auth = _conn()
    body: dict = {}
    if notification:
        body["notification"] = notification
    try:
        r = httpx.post(
            f"{base}/answers",
            params={"callback_id": callback_id},
            json=body,
            headers={auth: token, "Content-Type": "application/json"},
            timeout=20,
        )
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning("max_answer_failed", error=str(e))


def callback_buttons(post_id: int) -> list:
    """Клавиатура готового поста (шаг 2): опубликовать/править/отклонить."""
    return [
        [
            {"type": "callback", "text": "✅ Опубликовать", "payload": f"mod:approve:{post_id}"},
            {"type": "callback", "text": "✏️ Править", "payload": f"mod:edit:{post_id}"},
            {"type": "callback", "text": "❌ Отклонить", "payload": f"mod:reject:{post_id}"},
        ]
    ]


def preview_buttons(raw_id: int) -> list:
    """Клавиатура карточки-оригинала (шаг 1): переписать/отменить."""
    return [
        [
            {"type": "callback", "text": "✍️ Переписать", "payload": f"pre:rewrite:{raw_id}"},
            {"type": "callback", "text": "🗑 Отменить", "payload": f"pre:cancel:{raw_id}"},
        ]
    ]
