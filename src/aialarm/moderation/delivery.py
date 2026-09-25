"""Durable, leased delivery queue for original moderation cards.

Delivery is at least once: a crash after the remote send and before commit can
repeat a card. A failed request never silently removes a card from the queue.
"""

from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from aialarm.db import session_scope
from aialarm.db.models import DistrictPost, ModerationDelivery, NewsStatus, RawNews, utcnow
from aialarm.logging import get_logger

log = get_logger(__name__)


def enqueue_preview(session: Session, kind: str, object_id: int) -> None:
    session.add(ModerationDelivery(kind=kind, object_id=object_id))


def deliver_previews(kind: str, limit: int = 50) -> int:
    from aialarm.moderation.notify import send_district_preview, send_preview

    now = utcnow()
    with session_scope() as session:
        ids = list(
            session.scalars(
                select(ModerationDelivery.id)
                .where(
                    ModerationDelivery.kind == kind,
                    ModerationDelivery.status.in_(("pending", "sending")),
                    ModerationDelivery.next_attempt_at <= now,
                )
                .order_by(ModerationDelivery.id)
                .limit(limit)
            )
        )
    sent = 0
    for index, delivery_id in enumerate(ids):
        if index:
            time.sleep(4)
        with session_scope() as session:
            claimed = session.execute(
                update(ModerationDelivery)
                .where(
                    ModerationDelivery.id == delivery_id,
                    ModerationDelivery.status.in_(("pending", "sending")),
                    ModerationDelivery.next_attempt_at <= now,
                )
                .values(
                    status="sending",
                    next_attempt_at=utcnow() + timedelta(minutes=10),
                    attempts=ModerationDelivery.attempts + 1,
                )
            )
            if not claimed.rowcount:
                continue
            delivery = session.get(ModerationDelivery, delivery_id)
            object_id, attempts = delivery.object_id, delivery.attempts
            obj = session.get(RawNews if kind == "main" else DistrictPost, object_id)
            if not obj or obj.status != (NewsStatus.PREVIEW if kind == "main" else "preview"):
                delivery.status = "cancelled"
                continue
        try:
            (send_preview if kind == "main" else send_district_preview)(object_id)
        except Exception as exc:  # noqa: BLE001
            with session_scope() as session:
                delivery = session.get(ModerationDelivery, delivery_id)
                delivery.status = "pending"
                delivery.last_error = str(exc)[:1000]
                delivery.next_attempt_at = utcnow() + timedelta(
                    minutes=min(30, 2 ** min(attempts, 5))
                )
            log.warning("preview_delivery_retry", kind=kind, object_id=object_id, error=str(exc))
        else:
            with session_scope() as session:
                delivery = session.get(ModerationDelivery, delivery_id)
                delivery.status = "sent"
                delivery.last_error = ""
            sent += 1
    return sent
