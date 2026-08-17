"""Низкоуровневые вызовы MAX Bot API для модерации.

MAX (форк TamTam Bot API): авторизация заголовком Authorization, chat_id — query-параметр,
inline-кнопки — attachments типа inline_keyboard с кнопками type=callback. Нажатие
приходит апдейтом message_callback; ответ на него — POST /answers?callback_id=...
Домен и заголовок берём из config.max_platform (могут мигрировать).
"""
from __future__ import annotations

from pathlib import Path

import httpx

from aialarm.config import get_settings
from aialarm.logging import get_logger
from aialarm.media import MAX_IMAGES_PER_POST

log = get_logger(__name__)


def _conn() -> tuple[str, str, str]:
    s = get_settings()
    base = s.project.max_platform.base_url.rstrip("/")
    return base, s.secrets.max_bot_token, s.project.max_platform.auth_header


def _load_image_bytes(client: httpx.Client, ref: str) -> bytes | None:
    try:
        if not ref.startswith(("http://", "https://")):
            path = Path(ref)
            return path.read_bytes() if path.exists() else None
        response = client.get(ref, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        return response.content
    except Exception as e:  # noqa: BLE001
        log.warning("max_moderation_image_load_failed", ref=ref[:80], error=str(e))
        return None


def _upload_image(
    client: httpx.Client,
    base: str,
    headers: dict,
    image: bytes,
) -> dict | None:
    try:
        response = client.post(f"{base}/uploads", params={"type": "image"}, headers=headers)
        response.raise_for_status()
        upload_url = response.json().get("url")
        if not upload_url:
            return None
        uploaded = client.post(
            upload_url,
            files={"data": ("image.jpg", image, "image/jpeg")},
        )
        uploaded.raise_for_status()
        data = uploaded.json()
        if data.get("photos"):
            return {"photos": data["photos"]}
        if data.get("token"):
            return {"token": data["token"]}
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("max_moderation_image_upload_failed", error=str(e))
        return None


def send_message(
    chat_id: str,
    text: str,
    buttons: list | None = None,
    image_ref: str | None = None,
    image_refs: list[str] | None = None,
) -> dict:
    base, token, auth = _conn()
    headers = {auth: token}
    body: dict = {"text": text}
    attachments: list[dict] = []
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        refs = list(image_refs or []) + ([image_ref] if image_ref else [])
        unique_refs = list(dict.fromkeys(refs))[:MAX_IMAGES_PER_POST]
        for ref in unique_refs:
            image = _load_image_bytes(client, ref)
            if not image:
                continue
            payload = _upload_image(client, base, headers, image)
            if payload:
                attachments.append({"type": "image", "payload": payload})
        if buttons:
            attachments.append({"type": "inline_keyboard", "payload": {"buttons": buttons}})
        if attachments:
            body["attachments"] = attachments
        response = client.post(
            f"{base}/messages",
            params={"chat_id": str(chat_id)},
            json=body,
            headers={**headers, "Content-Type": "application/json"},
        )
    response.raise_for_status()
    return response.json() if response.content else {}


def edit_message(
    message_id: str,
    text: str,
    buttons: list | None = None,
    image_refs: list[str] | None = None,
) -> bool:
    """Обновить сообщение и при необходимости заново прикрепить весь фотоальбом."""
    base, token, auth = _conn()
    headers = {auth: token}
    attachments: list[dict] = []
    refs = list(dict.fromkeys(image_refs or []))[:MAX_IMAGES_PER_POST]
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            for ref in refs:
                image = _load_image_bytes(client, ref)
                if not image:
                    return False
                payload = _upload_image(client, base, headers, image)
                if not payload:
                    return False
                attachments.append({"type": "image", "payload": payload})
            if buttons:
                attachments.append(
                    {"type": "inline_keyboard", "payload": {"buttons": buttons}}
                )
            response = client.put(
                f"{base}/messages",
                params={"message_id": message_id},
                json={"text": text, "attachments": attachments, "notify": False},
                headers={**headers, "Content-Type": "application/json"},
            )
        if response.status_code >= 400:
            log.warning("max_edit_http_error", message_id=str(message_id)[:24],
                        status=response.status_code, body=response.text[:300])
            return False
        data = response.json() if response.content else {}
        ok = bool(data.get("success", True))
        if not ok:
            log.warning("max_edit_not_success", message_id=str(message_id)[:24],
                        body=str(data)[:300])
        return ok
    except Exception as e:  # noqa: BLE001
        log.warning("max_edit_failed", message_id=str(message_id)[:24], error=str(e))
        return False


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
    """Клавиатура готового поста: MAX отдельно или MAX вместе с Telegram."""
    return [
        [
            {"type": "callback", "text": "MAX", "payload": f"mod:approve_max:{post_id}"},
            {"type": "callback", "text": "MAX + ТГ", "payload": f"mod:approve_all:{post_id}"},
        ],
        [
            {"type": "callback", "text": "✏️ Править", "payload": f"mod:edit:{post_id}"},
            {"type": "callback", "text": "❌ Отклонить", "payload": f"mod:reject:{post_id}"},
        ],
        [
            {"type": "callback", "text": "🖼 Изменить картинку", "payload": f"mod:media:{post_id}"},
        ],
    ]


def visual_choice_buttons(post_id: int, post: dict) -> list:
    """Экран осознанного выбора визуала для городского поста."""
    recommended = post.get("visual_recommendation", "")
    original_label = "📷 Оригинал" + (" ✓" if recommended == "original" else "")
    generated_label = "✨ Генерация" + (" ✓" if recommended == "generate" else "")
    none_label = "🚫 Без картинки" + (" ✓" if recommended == "none" else "")
    rows: list[list[dict]] = []
    if post.get("has_original_image"):
        rows.append([
            {"type": "callback", "text": original_label, "payload": f"mod:original:{post_id}"},
        ])
    if post.get("generation_available"):
        rows.append([
            {"type": "callback", "text": generated_label, "payload": f"mod:generate:{post_id}"},
        ])
    rows.append([
        {"type": "callback", "text": none_label, "payload": f"mod:none:{post_id}"},
    ])
    return rows


def preview_buttons(raw_id: int) -> list:
    """Клавиатура карточки-оригинала (шаг 1): переписать/отменить."""
    return [
        [
            {"type": "callback", "text": "✍️ Переписать", "payload": f"pre:rewrite:{raw_id}"},
            {"type": "callback", "text": "🗑 Отменить", "payload": f"pre:cancel:{raw_id}"},
        ]
    ]


def control_buttons(profile: str = "test") -> list:
    """Панель сменного режима и контура публикации."""
    test_label = "🧪 Тест ✓" if profile == "test" else "🧪 Тест"
    main_label = "🚀 Основной ✓" if profile == "main" else "🚀 Основной"
    return [
        [
            {"type": "callback", "text": "▶️ Включить", "payload": "ctl:on"},
            {"type": "callback", "text": "⏸ Выключить", "payload": "ctl:off"},
        ],
        [
            {"type": "callback", "text": "🕒 AUTO", "payload": "ctl:auto"},
            {"type": "callback", "text": "📊 Статус", "payload": "ctl:status"},
        ],
        [
            {"type": "callback", "text": test_label, "payload": "ctl:profile_test"},
            {"type": "callback", "text": main_label, "payload": "ctl:profile_main"},
        ],
        [
            {"type": "callback", "text": "✍️ Свой пост", "payload": "ctl:own"},
        ],
    ]


def district_control_buttons(profile: str = "test") -> list:
    test_label = "🧪 Тест ✓" if profile == "test" else "🧪 Тест"
    main_label = "🚀 Основной ✓" if profile == "main" else "🚀 Основной"
    return [
        [
            {"type": "callback", "text": "▶️ Включить", "payload": "dctl:on"},
            {"type": "callback", "text": "⏸ Выключить", "payload": "dctl:off"},
        ],
        [
            {"type": "callback", "text": "🕒 AUTO", "payload": "dctl:auto"},
            {"type": "callback", "text": "📊 Статистика", "payload": "dctl:statistics"},
        ],
        [
            {"type": "callback", "text": test_label, "payload": "dctl:profile_test"},
            {"type": "callback", "text": main_label, "payload": "dctl:profile_main"},
        ],
    ]


def district_quota_buttons(district_id: str) -> list:
    """Решение после выполнения дневной нормы конкретного района."""
    return [[
        {"type": "callback", "text": "⏸ Остановить поиск", "payload": f"dquota:stop:{district_id}"},
        {"type": "callback", "text": "▶️ Продолжить", "payload": f"dquota:continue:{district_id}"},
    ]]


def district_preview_buttons(post_id: int) -> list:
    return [
        [
            {"type": "callback", "text": "✍️ Переписать", "payload": f"dpre:rewrite:{post_id}"},
            {"type": "callback", "text": "🗑 Отменить", "payload": f"dpre:cancel:{post_id}"},
        ]
    ]


def district_callback_buttons(post_id: int) -> list:
    return [
        [
            {"type": "callback", "text": "MAX", "payload": f"dmod:approve_max:{post_id}"},
            {"type": "callback", "text": "MAX + ТГ", "payload": f"dmod:approve_all:{post_id}"},
        ],
        [
            {"type": "callback", "text": "✏️ Править", "payload": f"dmod:edit:{post_id}"},
            {"type": "callback", "text": "❌ Отклонить", "payload": f"dmod:reject:{post_id}"},
        ],
    ]
