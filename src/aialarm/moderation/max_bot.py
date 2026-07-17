"""Бот-модератор на стороне MAX (long-polling).

Обрабатывает нажатия callback-кнопок карточек (✅/✏️/❌) и правки текста.
Аналог aiogram-бота, но через HTTP MAX Bot API. Запуск: python -m aialarm.cli bot
(при moderation.platform == "max" CLI поднимает именно этот бот).

Логика:
- ✅ approve  -> утвердить и опубликовать в каналы (publish.targets);
- ❌ reject   -> отклонить;
- ✏️ edit     -> ждём следующее сообщение этого пользователя как новый текст, затем публикуем.

Маркер long-polling держим в памяти; на старте «сматываем» накопленные апдейты,
чтобы не выполнять старые нажатия повторно после перезапуска.
"""
from __future__ import annotations

import time

from aialarm.config import get_settings
from aialarm.logging import get_logger
from aialarm.moderation import max_client, service
from aialarm.moderation.notify import send_card
from aialarm.publishers.service import publish_post_id_sync

log = get_logger(__name__)

# user_id -> post_id, ожидающий исправленного текста
_edit_state: dict[int, int] = {}


def _handle_callback(update: dict) -> None:
    cb = update.get("callback") or {}
    payload = cb.get("payload", "")
    cid = cb.get("callback_id", "")
    user_id = (cb.get("user") or {}).get("user_id")
    mid = ((update.get("message") or {}).get("body") or {}).get("mid")
    prefix, action, id_s = (payload.split(":") + ["", ""])[:3]
    obj_id = int(id_s) if id_s.isdigit() else None

    # ── Шаг 1: карточка-оригинал ──────────────────────────────────────────
    if prefix == "pre" and obj_id is not None:
        if action == "rewrite":
            max_client.answer_callback(cid, "✍️ Переписываю…")
            post_id = service.rewrite_and_get(obj_id)
            if mid:
                max_client.delete_message(mid)  # убираем карточку-оригинал
            if post_id:
                send_card(post_id)              # шлём готовый пост со схемой ✅/✏️/❌
        elif action == "cancel":
            service.cancel_preview(obj_id)
            if mid:
                max_client.delete_message(mid)
            max_client.answer_callback(cid, "🗑 Отменено")
        return

    # ── Шаг 2: готовый пост ───────────────────────────────────────────────
    if prefix != "mod" or obj_id is None:
        return
    post_id = obj_id
    if action == "approve":
        if service.approve(post_id):
            ok = publish_post_id_sync(post_id)
            max_client.answer_callback(cid, "✅ Опубликовано" if ok else "Одобрено, но публикация не удалась")
        else:
            max_client.answer_callback(cid, "Уже обработано")
    elif action == "reject":
        service.reject(post_id)
        max_client.answer_callback(cid, "❌ Отклонено")
    elif action == "edit":
        if user_id is not None:
            _edit_state[user_id] = post_id
        max_client.answer_callback(cid, "✏️ Пришлите исправленный текст сообщением")


def _handle_message(msg: dict) -> None:
    sender = msg.get("sender") or {}
    user_id = sender.get("user_id")
    text = (msg.get("body") or {}).get("text", "")
    if user_id in _edit_state and text:
        post_id = _edit_state.pop(user_id)
        service.apply_edit(post_id, text)
        ok = publish_post_id_sync(post_id)
        chat = get_settings().project.moderation.max_chat_id
        max_client.send_message(
            chat, "✅ Исправлено и опубликовано" if ok else "Исправлено. Публикация не удалась (см. логи)"
        )


def _dispatch(update: dict) -> None:
    t = update.get("update_type")
    if t == "message_callback":
        _handle_callback(update)
    elif t == "message_created":
        _handle_message(update.get("message") or {})


def run() -> None:
    if not get_settings().secrets.max_bot_token:
        raise RuntimeError("MAX_BOT_TOKEN не задан")
    # Сматываем старые апдейты, чтобы не повторять действия после рестарта.
    marker: int | None = None
    try:
        init = max_client.get_updates(timeout=0)
        marker = init.get("marker")
    except Exception as e:  # noqa: BLE001
        log.warning("max_updates_init_failed", error=str(e))

    log.info("max_moderation_bot_start", marker=marker)
    while True:
        try:
            data = max_client.get_updates(marker=marker, timeout=30)
        except Exception as e:  # noqa: BLE001
            log.warning("max_updates_failed", error=str(e))
            time.sleep(3)
            continue
        for upd in data.get("updates", []):
            try:
                _dispatch(upd)
            except Exception as e:  # noqa: BLE001
                log.error("max_update_handle_failed", error=str(e))
        marker = data.get("marker", marker)


def main() -> None:
    run()
