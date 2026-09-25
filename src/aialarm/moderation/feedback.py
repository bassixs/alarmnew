"""Editor decisions and immutable text history."""

from __future__ import annotations

from sqlalchemy.orm import Session

from aialarm.db.models import ModerationFeedback

REJECTION_REASONS = {
    "irrelevant": "Не подходит аудитории",
    "minor": "Слишком мелкое событие",
    "region": "Не наш регион",
    "duplicate": "Дубль",
    "outdated": "Устарело",
    "facts": "Ошибка в фактах",
    "style": "Неудачный текст",
    "other": "Другая причина",
}


def record_feedback(
    session: Session,
    *,
    raw_id: int,
    action: str,
    stage: str = "rewrite",
    post_id: int | None = None,
    district_post_id: int | None = None,
    editor_id: int | str | None = None,
    platform: str = "",
    reason: str = "",
    model: str = "",
    text_before: str | None = None,
    text_after: str | None = None,
) -> None:
    session.add(
        ModerationFeedback(
            raw_id=raw_id,
            post_id=post_id,
            district_post_id=district_post_id,
            stage=stage,
            action=action,
            editor_id=str(editor_id) if editor_id is not None else "",
            platform=platform,
            reason=reason,
            model=model,
            text_before=text_before,
            text_after=text_after,
        )
    )


def reason_buttons(prefix: str, action: str, object_id: int) -> list:
    rows = [
        [
            {
                "type": "callback",
                "text": label,
                "payload": f"{prefix}:{action}:{object_id}:{code}",
            }
        ]
        for code, label in REJECTION_REASONS.items()
    ]
    rows.append(
        [
            {
                "type": "callback",
                "text": "↩️ Назад",
                "payload": f"{prefix}:{action}:{object_id}:back",
            }
        ]
    )
    return rows
