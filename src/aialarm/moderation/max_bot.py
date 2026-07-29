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
from aialarm.control import get_pipeline_state, render_control_status, set_control_mode
from aialarm.logging import get_logger
from aialarm.moderation import max_client, service
from aialarm.moderation.notify import edit_card, finalize_card, send_card
from aialarm.publishers.service import publish_post_id_sync

log = get_logger(__name__)

# user_id -> (post_id, message_id карточки), ожидающий исправленного текста
_edit_state: dict[int, tuple[int, str]] = {}


def _control_allowed(user_id: int | None) -> bool:
    allowed = get_settings().project.moderation.control_user_ids
    return not allowed or (user_id is not None and user_id in allowed)


def _control_notice(action: str, active: bool) -> str:
    if action == "auto":
        detail = (
            "Сейчас помощник работает по расписанию."
            if active
            else "Сейчас помощник вне смены и включится автоматически по расписанию."
        )
        return f"🕒 Режим AUTO включён\n\n{detail}"
    if action == "on":
        return "▶️ Помощник включён вручную"
    if action == "off":
        return "⏸ Помощник выключен вручную"
    return ""


def _send_control_panel(message_id: str = "", notice: str = "") -> None:
    text = render_control_status()
    if notice:
        text = f"{notice}\n\n{text}"
    buttons = max_client.control_buttons()
    if message_id and max_client.edit_message(message_id, text, buttons=buttons):
        return
    chat = get_settings().project.moderation.max_chat_id
    if chat:
        max_client.send_message(chat, text, buttons=buttons)


def _handle_control(
    action: str,
    user_id: int | None,
    callback_id: str = "",
    message_id: str = "",
) -> None:
    if not _control_allowed(user_id):
        if callback_id:
            max_client.answer_callback(callback_id, "Нет доступа к управлению")
        return
    notice = ""
    if action in {"on", "off", "auto"}:
        state = set_control_mode(action)
        notice = _control_notice(action, state.active)
        if callback_id:
            callback_text = (
                "Режим AUTO включён"
                if action == "auto"
                else f"Помощник {'включён' if state.active else 'выключен'}"
            )
            max_client.answer_callback(callback_id, callback_text)
    elif action == "status" and callback_id:
        max_client.answer_callback(callback_id, "Статус обновлён")
    _send_control_panel(message_id=message_id, notice=notice)


def _handle_callback(update: dict) -> None:
    cb = update.get("callback") or {}
    payload = cb.get("payload", "")
    cid = cb.get("callback_id", "")
    user_id = (cb.get("user") or {}).get("user_id")
    mid = ((update.get("message") or {}).get("body") or {}).get("mid")
    prefix, action, id_s = (payload.split(":") + ["", ""])[:3]
    obj_id = int(id_s) if id_s.isdigit() else None

    if prefix == "ctl":
        _handle_control(action, user_id, cid, mid or "")
        return

    # ── Шаг 1: карточка-оригинал ──────────────────────────────────────────
    if prefix == "pre" and obj_id is not None:
        if action == "rewrite":
            if not get_pipeline_state().active:
                max_client.answer_callback(cid, "Помощник сейчас вне смены")
                return
            max_client.answer_callback(cid, "✍️ Переписываю…")
            post_id = service.rewrite_and_get(obj_id)
            if post_id:
                converted = bool(mid and edit_card(post_id, mid))
                if not converted:
                    # Запасной путь: сначала успешно отправляем новую карточку,
                    # только затем убираем оригинал, чтобы ничего не потерять.
                    send_card(post_id)
                    if mid:
                        max_client.delete_message(mid)
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
        if not get_pipeline_state().active:
            max_client.answer_callback(cid, "Помощник сейчас вне смены")
            return
        if service.approve(post_id):
            ok = publish_post_id_sync(post_id)
            header = "✅ ОПУБЛИКОВАНО" if ok else "⚠️ Одобрено, публикация не удалась (см. логи)"
            finalize_card(post_id, mid or "", header)
            max_client.answer_callback(cid, "✅ Опубликовано" if ok else "Публикация не удалась")
        else:
            max_client.answer_callback(cid, "Уже обработано")
    elif action == "reject":
        service.reject(post_id)
        finalize_card(post_id, mid or "", "❌ ОТКЛОНЕНО")
        max_client.answer_callback(cid, "❌ Отклонено")
    elif action == "edit":
        if not get_pipeline_state().active:
            max_client.answer_callback(cid, "Помощник сейчас вне смены")
            return
        if user_id is not None:
            _edit_state[user_id] = (post_id, mid or "")
        # Убираем кнопки и показываем, что ждём текст (повторно нажать нельзя).
        finalize_card(post_id, mid or "", "✏️ ЖДУ ИСПРАВЛЕННЫЙ ТЕКСТ…")
        max_client.answer_callback(cid, "✏️ Пришлите исправленный текст сообщением")


def _handle_message(msg: dict) -> None:
    sender = msg.get("sender") or {}
    user_id = sender.get("user_id")
    text = (msg.get("body") or {}).get("text", "")
    command = text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower() if text.strip() else ""
    control_actions = {
        "/start": "status",
        "/bot": "status",
        "/status": "status",
        "/bot_on": "on",
        "/bot_off": "off",
        "/bot_auto": "auto",
    }
    if command in control_actions:
        _handle_control(control_actions[command], user_id)
        return
    if user_id in _edit_state and text:
        post_id, card_mid = _edit_state.pop(user_id)
        service.apply_edit(post_id, text)
        ok = publish_post_id_sync(post_id)
        header = (
            "✅ ОПУБЛИКОВАНО (с правкой редактора)"
            if ok
            else "⚠️ Исправлено, публикация не удалась (см. логи)"
        )
        # Редактируем исходную карточку в финальное состояние; если не вышло — шлём статус.
        if not finalize_card(post_id, card_mid, header):
            chat = get_settings().project.moderation.max_chat_id
            if chat:
                max_client.send_message(chat, header)


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
        updates = data.get("updates", [])
        for upd in updates:
            try:
                _dispatch(upd)
            except Exception as e:  # noqa: BLE001
                log.error("max_update_handle_failed", error=str(e))
        marker = data.get("marker", marker)
        if not updates:
            time.sleep(2)  # long-poll не всегда держит соединение — не молотим API вхолостую


def main() -> None:
    run()
