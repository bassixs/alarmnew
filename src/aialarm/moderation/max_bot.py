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

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import time

from aialarm.config import district_for_id, get_settings
from aialarm.control import (
    get_district_publish_profile,
    get_district_pipeline_state,
    get_pipeline_state,
    get_publish_profile,
    render_control_status,
    render_district_daily_statistics,
    render_district_control_status,
    set_control_mode,
    set_district_publish_profile,
    set_district_control_mode,
    set_publish_profile,
)
from aialarm.logging import get_logger
from aialarm.moderation import districts, max_client, service
from aialarm.moderation.notify import (
    edit_card, edit_district_card, finalize_card, finalize_district_card, send_card,
)
from aialarm.publishers.service import publish_post_id_sync

log = get_logger(__name__)

# user_id -> (post_id, message_id карточки), ожидающий исправленного текста
_edit_state: dict[int, tuple[int, str]] = {}
_district_edit_state: dict[int, tuple[int, str]] = {}
# user_id ожидает текст собственного городского поста из панели /bot.
_own_post_state: set[int] = set()

# Long-polling должен быстро подтверждать callback: LLM-рерайт, публикация на две
# площадки и обновление карточки могут занять несколько секунд. Один post_id обрабатываем
# только один раз, чтобы повторный тап не создал дубль публикации.
_actions = ThreadPoolExecutor(max_workers=3, thread_name_prefix="max-action")
_inflight_posts: set[tuple[str, int]] = set()
_inflight_lock = Lock()


def _submit_post_action(
    post_id: int,
    action: Callable[[], None],
    callback_id: str,
    confirmation: str,
) -> bool:
    """Подтвердить callback до запуска тяжёлой задачи и не допустить дубль."""
    key = ("main", post_id)
    with _inflight_lock:
        if key in _inflight_posts:
            return False
        _inflight_posts.add(key)

    def run() -> None:
        try:
            action()
        except Exception as e:  # noqa: BLE001
            log.error("max_background_action_failed", post_id=post_id, error=str(e))
        finally:
            with _inflight_lock:
                _inflight_posts.discard(key)

    try:
        # Это снимает индикатор ожидания в MAX до LLM/API-вызовов ниже.
        max_client.answer_callback(callback_id, confirmation)
        _actions.submit(run)
    except Exception:  # noqa: BLE001
        with _inflight_lock:
            _inflight_posts.discard(key)
        raise
    return True


def _submit_district_action(
    post_id: int, action: Callable[[], None], callback_id: str, confirmation: str
) -> bool:
    key = ("district", post_id)
    with _inflight_lock:
        if key in _inflight_posts:
            return False
        _inflight_posts.add(key)

    def run() -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001
            log.error("district_background_action_failed", post_id=post_id, error=str(exc))
        finally:
            with _inflight_lock:
                _inflight_posts.discard(key)

    try:
        max_client.answer_callback(callback_id, confirmation)
        _actions.submit(run)
    except Exception:
        with _inflight_lock:
            _inflight_posts.discard(key)
        raise
    return True


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
    if action == "profile_test":
        return "🧪 Выбран тестовый контур публикации"
    if action == "profile_main":
        return "🚀 Выбран основной контур публикации"
    return ""


def _send_control_panel(message_id: str = "", notice: str = "") -> None:
    text = render_control_status()
    if notice:
        text = f"{notice}\n\n{text}"
    buttons = max_client.control_buttons(get_publish_profile())
    if message_id and max_client.edit_message(message_id, text, buttons=buttons):
        return
    chat = get_settings().project.moderation.max_chat_id
    if chat:
        max_client.send_message(chat, text, buttons=buttons)


def _send_district_control_panel(message_id: str = "", notice: str = "") -> None:
    text = render_district_control_status(include_summary=False)
    if notice:
        text = f"{notice}\n\n{text}"
    buttons = max_client.district_control_buttons(get_district_publish_profile())
    if message_id and max_client.edit_message(message_id, text, buttons=buttons):
        return
    chat = get_settings().project.districts.moderation_max_chat_id
    if chat:
        max_client.send_message(chat, text, buttons=buttons)


def _send_district_statistics() -> None:
    chat = get_settings().project.districts.moderation_max_chat_id
    if chat:
        max_client.send_message(chat, render_district_daily_statistics())


def _handle_district_control(
    action: str, user_id: int | None, callback_id: str, message_id: str
) -> None:
    if not _control_allowed(user_id):
        max_client.answer_callback(callback_id, "Нет доступа к управлению")
        return
    if action in {"on", "off", "auto"}:
        state = set_district_control_mode(action)
        notice = (
            "▶️ Районный помощник включён вручную"
            if action == "on"
            else "⏸ Районный помощник выключен вручную"
            if action == "off"
            else "🕒 Районный помощник работает по расписанию"
        )
        callback_text = (
            "AUTO"
            if action == "auto"
            else "Включён"
            if state.active
            else "Выключен"
        )
        max_client.answer_callback(callback_id, callback_text)
    elif action in {"profile_test", "profile_main"}:
        profile = set_district_publish_profile("test" if action == "profile_test" else "main")
        max_client.answer_callback(callback_id, "Тестовый контур" if profile == "test" else "Основной контур")
        notice = "🧪 Выбран тестовый контур" if profile == "test" else "🚀 Выбран основной контур"
    elif action == "statistics":
        max_client.answer_callback(callback_id, "Статистика отправлена отдельным сообщением")
        _send_district_statistics()
        return
    elif action == "status":  # совместимость со старыми кнопками в чате
        max_client.answer_callback(callback_id, "Статус обновлён")
        notice = ""
    else:
        return
    _send_district_control_panel(message_id, notice)


def _handle_district_quota(
    action: str, district_id: str, user_id: int | None, callback_id: str, message_id: str
) -> None:
    if not _control_allowed(user_id):
        max_client.answer_callback(callback_id, "Нет доступа к управлению")
        return
    if action not in {"stop", "continue"} or not districts.set_district_search_mode(
        district_id, action
    ):
        max_client.answer_callback(callback_id, "Не удалось изменить поиск района")
        return
    district = district_for_id(district_id)
    title = district.title if district else district_id
    if action == "continue":
        text = (
            f"▶️ {title}: поиск продолжен до конца дня. "
            "Можно публиковать новости сверх дневной нормы."
        )
        callback_text = "Поиск продолжен"
    else:
        text = f"⏸ {title}: поиск остановлен до конца дня."
        callback_text = "Поиск остановлен"
    max_client.answer_callback(callback_id, callback_text)
    if message_id:
        max_client.edit_message(message_id, text)


def _send_district_quota_notice(district_id: str) -> None:
    district = district_for_id(district_id)
    if not district:
        return
    chat = get_settings().project.districts.moderation_max_chat_id
    if not chat:
        return
    limit = districts.current_district_post_limit()
    text = (
        f"🏘 {district.title}: дневная норма выполнена — {limit} пост(а).\n\n"
        "Поиск по району поставлен на паузу до конца дня. При необходимости "
        "можно продолжить поиск и публиковать новости сверх нормы."
    )
    try:
        max_client.send_message(chat, text, buttons=max_client.district_quota_buttons(district_id))
    except Exception as exc:  # noqa: BLE001
        log.error("district_quota_notice_failed", district=district_id, error=str(exc))


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
    elif action in {"profile_test", "profile_main"}:
        profile = set_publish_profile("test" if action == "profile_test" else "main")
        notice = _control_notice(action, True)
        if callback_id:
            max_client.answer_callback(
                callback_id,
                "Тестовый контур" if profile == "test" else "Основной контур",
            )
    elif action == "own":
        if not get_pipeline_state().active:
            if callback_id:
                max_client.answer_callback(callback_id, "Помощник сейчас вне смены")
            return
        if user_id is None:
            if callback_id:
                max_client.answer_callback(callback_id, "Не удалось определить пользователя")
            return
        _own_post_state.add(user_id)
        if callback_id:
            max_client.answer_callback(callback_id, "Пришлите текст своего поста")
        chat = get_settings().project.moderation.max_chat_id
        if chat:
            max_client.send_message(
                chat,
                "✍️ Пришлите текст своего поста следующим сообщением. "
                "Я перепишу его в стиле канала и верну на согласование.",
            )
        return
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
    if prefix == "dctl":
        _handle_district_control(action, user_id, cid, mid or "")
        return
    if prefix == "dquota":
        _handle_district_quota(action, id_s, user_id, cid, mid or "")
        return

    # ── Районный контур: отдельные карточки и публикация ровно в один канал ──
    if prefix == "dpre" and obj_id is not None:
        if action == "rewrite":
            if not get_district_pipeline_state().active:
                max_client.answer_callback(cid, "Районный помощник сейчас вне смены")
                return
            if not _submit_district_action(
                obj_id, lambda: _rewrite_district_preview(obj_id, mid or ""), cid, "✍️ Переписываю…"
            ):
                max_client.answer_callback(cid, "⏳ Уже переписываю…")
        elif action == "cancel":
            if not _submit_district_action(
                obj_id, lambda: _cancel_district_post(obj_id, mid or ""), cid, "🗑 Отменено"
            ):
                max_client.answer_callback(cid, "⏳ Уже обрабатываю…")
        return

    if prefix == "dmod" and obj_id is not None:
        if action in {"approve", "approve_max", "approve_all"}:
            if not get_district_pipeline_state().active:
                max_client.answer_callback(cid, "Районный помощник сейчас вне смены")
                return
            selected_targets = ["max"] if action in {"approve", "approve_max"} else ["max", "telegram"]
            confirmation = "⏳ Публикую в MAX…" if selected_targets == ["max"] else "⏳ Публикую в MAX и ТГ…"
            if not _submit_district_action(
                obj_id,
                lambda: _publish_district_post(obj_id, mid or "", selected_targets),
                cid,
                confirmation,
            ):
                max_client.answer_callback(cid, "⏳ Уже публикую…")
        elif action == "reject":
            if not _submit_district_action(
                obj_id, lambda: _reject_district_post(obj_id, mid or ""), cid, "❌ Отклонено"
            ):
                max_client.answer_callback(cid, "⏳ Уже обрабатываю…")
        elif action == "edit":
            if not get_district_pipeline_state().active:
                max_client.answer_callback(cid, "Районный помощник сейчас вне смены")
                return
            if user_id is not None:
                _district_edit_state[user_id] = (obj_id, mid or "")
            max_client.answer_callback(cid, "✏️ Пришлите исправленный текст сообщением")
            finalize_district_card(obj_id, mid or "", "✏️ ЖДУ ИСПРАВЛЕННЫЙ ТЕКСТ…")
        return

    # ── Шаг 1: карточка-оригинал ──────────────────────────────────────────
    if prefix == "pre" and obj_id is not None:
        if action == "rewrite":
            if not get_pipeline_state().active:
                max_client.answer_callback(cid, "Помощник сейчас вне смены")
                return
            if not _submit_post_action(
                obj_id, lambda: _rewrite_preview(obj_id, mid or ""), cid, "✍️ Переписываю…"
            ):
                max_client.answer_callback(cid, "⏳ Уже переписываю…")
                return
        elif action == "cancel":
            if not _submit_post_action(
                obj_id, lambda: _cancel_preview(obj_id, mid or ""), cid, "🗑 Отменено"
            ):
                max_client.answer_callback(cid, "⏳ Уже обрабатываю…")
                return
        return

    # ── Шаг 2: готовый пост ───────────────────────────────────────────────
    if prefix != "mod" or obj_id is None:
        return
    post_id = obj_id
    if action in {"approve", "approve_max", "approve_all"}:
        if not get_pipeline_state().active:
            max_client.answer_callback(cid, "Помощник сейчас вне смены")
            return
        selected_targets = ["max"] if action == "approve_max" else None
        confirmation = "⏳ Публикую в MAX…" if selected_targets else "⏳ Публикую в MAX и ТГ…"
        if not _submit_post_action(
            post_id,
            lambda: _approve_and_publish(post_id, mid or "", selected_targets),
            cid,
            confirmation,
        ):
            max_client.answer_callback(cid, "⏳ Уже публикую…")
            return
    elif action == "reject":
        if not _submit_post_action(
            post_id, lambda: _reject_post(post_id, mid or ""), cid, "❌ Отклонено"
        ):
            max_client.answer_callback(cid, "⏳ Уже обрабатываю…")
            return
    elif action == "edit":
        if not get_pipeline_state().active:
            max_client.answer_callback(cid, "Помощник сейчас вне смены")
            return
        if user_id is not None:
            _edit_state[user_id] = (post_id, mid or "")
        # Снимаем индикатор нажатия до сетевого обновления карточки.
        max_client.answer_callback(cid, "✏️ Пришлите исправленный текст сообщением")
        # Убираем кнопки и показываем, что ждём текст (повторно нажать нельзя).
        finalize_card(post_id, mid or "", "✏️ ЖДУ ИСПРАВЛЕННЫЙ ТЕКСТ…")


def _rewrite_preview(raw_id: int, message_id: str) -> None:
    post_id = service.rewrite_and_get(raw_id)
    if not post_id:
        return
    converted = bool(message_id and edit_card(post_id, message_id))
    if not converted:
        # Запасной путь: сначала успешно отправляем новую карточку, только затем
        # убираем оригинал — так карточка не потеряется при проблеме с MAX API.
        send_card(post_id)
        if message_id:
            max_client.delete_message(message_id)


def _cancel_preview(raw_id: int, message_id: str) -> None:
    service.cancel_preview(raw_id)
    if message_id:
        max_client.delete_message(message_id)


def _rewrite_district_preview(post_id: int, message_id: str) -> None:
    if not districts.rewrite_district_post(post_id):
        return
    if not edit_district_card(post_id, message_id):
        log.warning("district_card_edit_failed", post_id=post_id)


def _cancel_district_post(post_id: int, message_id: str) -> None:
    if districts.cancel_district_post(post_id) and message_id:
        max_client.delete_message(message_id)


def _publish_district_post(
    post_id: int, message_id: str, selected_targets: list[str]
) -> None:
    ok, reason = districts.publish_district_post(post_id, selected_targets)
    if ok:
        finalize_district_card(post_id, message_id, "✅ ОПУБЛИКОВАНО")
    else:
        # Кнопки остаются: если одна площадка временно недоступна, повтор уйдёт
        # только на неё и не создаст дубль на уже успешной.
        edit_district_card(post_id, message_id, f"⚠️ НЕ ОПУБЛИКОВАНО: {reason}")
    if ok and reason == "daily_target_reached":
        post = districts.get_district_pending(post_id)
        if post:
            _send_district_quota_notice(post["district_id"])


def _reject_district_post(post_id: int, message_id: str) -> None:
    if districts.cancel_district_post(post_id):
        finalize_district_card(post_id, message_id, "❌ ОТКЛОНЕНО")


def _approve_and_publish(
    post_id: int,
    message_id: str,
    selected_targets: list[str] | None = None,
) -> None:
    if not service.approve(post_id):
        return
    ok = publish_post_id_sync(post_id, selected_targets)
    header = (
        "✅ ОПУБЛИКОВАНО"
        if ok
        else "⚠️ Не опубликовано на всех площадках — повтор будет автоматически"
    )
    finalize_card(post_id, message_id, header)


def _reject_post(post_id: int, message_id: str) -> None:
    service.reject(post_id)
    finalize_card(post_id, message_id, "❌ ОТКЛОНЕНО")


def _create_own_post(text: str) -> None:
    try:
        post_id = service.create_manual_post(text)
        if not post_id:
            return
        send_card(post_id)
    except Exception as exc:  # noqa: BLE001
        log.error("manual_post_create_failed", error=str(exc))
        chat = get_settings().project.moderation.max_chat_id
        if chat:
            max_client.send_message(chat, "⚠️ Не удалось переписать свой пост. Попробуйте ещё раз.")


def _handle_message(msg: dict) -> None:
    sender = msg.get("sender") or {}
    user_id = sender.get("user_id")
    text = (msg.get("body") or {}).get("text", "")
    command = text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower() if text.strip() else ""
    chat_id = str((msg.get("recipient") or {}).get("chat_id") or "")
    control_actions = {
        "/start": "status",
        "/bot": "status",
        "/status": "status",
        "/bot_on": "on",
        "/bot_off": "off",
        "/bot_auto": "auto",
    }
    if command in control_actions:
        if chat_id and chat_id == get_settings().project.districts.moderation_max_chat_id:
            _send_district_control_panel()
            return
        _handle_control(control_actions[command], user_id)
        return
    if user_id in _district_edit_state and text:
        post_id, card_mid = _district_edit_state.pop(user_id)
        if not districts.edit_district_post(post_id, text):
            log.warning("district_edit_save_failed", post_id=post_id)
            return
        if not edit_district_card(post_id, card_mid):
            log.warning("district_edit_card_failed", post_id=post_id)
        return
    main_chat = get_settings().project.moderation.max_chat_id
    if user_id in _own_post_state and text and chat_id == main_chat:
        _own_post_state.discard(user_id)
        if not get_pipeline_state().active:
            max_client.send_message(main_chat, "⏸ Помощник сейчас вне смены. Включите его в панели /bot.")
            return
        max_client.send_message(main_chat, "✍️ Переписываю свой пост…")
        _actions.submit(_create_own_post, text)
        return
    if user_id in _edit_state and text:
        post_id, card_mid = _edit_state.pop(user_id)
        if not service.apply_edit(post_id, text):
            log.warning("edit_save_failed", post_id=post_id)
            return
        # Возвращаем редактору тот же выбор площадки уже с исправленным текстом.
        updated = bool(card_mid and edit_card(post_id, card_mid))
        if not updated:
            send_card(post_id)
        log.info("edit_returned_to_moderation", post_id=post_id, card_updated=updated)


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
