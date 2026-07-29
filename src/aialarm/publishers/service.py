"""Оркестрация публикации: лимиты частоты + запись результатов в publications.

Лимиты из ТЗ/конфига: не больше max_posts_per_day и не чаще min_minutes_between_posts.
Пер-платформенные API-лимиты обрабатываются в адаптерах (rate_limited в PublishResult).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aialarm.config import get_settings
from aialarm.db import session_scope
from aialarm.db.models import (
    NewsStatus,
    Publication,
    PublishStatus,
    RawNews,
    RewrittenPost,
)
from aialarm.logging import get_logger
from aialarm.media import raw_image_refs
from aialarm.publishers.base import Post, get_publisher

log = get_logger(__name__)


def _today_success_count(session: Session) -> int:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        session.scalar(
            select(func.count(func.distinct(Publication.post_id)))
            .where(Publication.status == PublishStatus.SUCCESS)
            .where(Publication.published_at >= start)
        )
        or 0
    )


def _last_publish_at(session: Session) -> datetime | None:
    return session.scalar(
        select(func.max(Publication.published_at)).where(
            Publication.status == PublishStatus.SUCCESS
        )
    )


def can_publish_now(session: Session, *, retrying: bool = False) -> tuple[bool, str]:
    pub = get_settings().project.publish
    # Доставку уже начатого поста не блокируем дневной квотой: иначе при успехе
    # Telegram и сбое MAX второй канал может не дождаться повторной попытки до завтра.
    if not retrying and _today_success_count(session) >= pub.max_posts_per_day:
        return False, "достигнут дневной лимит постов"
    last = _last_publish_at(session)
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - last
        if delta < timedelta(minutes=pub.min_minutes_between_posts):
            return False, "не выдержан интервал между постами"
    return True, ""


def _to_post(rp: RewrittenPost) -> Post:
    images = raw_image_refs(rp.raw)
    return Post(
        text=rp.post_text,
        image_url=images[0] if images else None,
        image_urls=images,
        hashtags=list(rp.hashtags or []),
    )


def _targets() -> list[str]:
    """Активные площадки без дублей и с сохранением порядка из конфига."""
    return list(dict.fromkeys(get_settings().project.publish.targets))


def _successful_platforms(session: Session, post_id: int) -> set[str]:
    return set(
        session.scalars(
            select(Publication.platform).where(
                Publication.post_id == post_id,
                Publication.status == PublishStatus.SUCCESS,
            )
        ).all()
    )


def _completed_platforms(session: Session, post_id: int) -> set[str]:
    """Площадки, на которых пост уже доставлен или сознательно не запрошен."""
    return set(
        session.scalars(
            select(Publication.platform).where(
                Publication.post_id == post_id,
                Publication.status.in_((PublishStatus.SUCCESS, PublishStatus.SKIPPED)),
            )
        ).all()
    )


def _pending_targets(session: Session, post_id: int) -> list[str]:
    completed = _completed_platforms(session, post_id)
    return [platform for platform in _targets() if platform not in completed]


def _skip_unselected_targets(session: Session, post_id: int, selected: list[str]) -> None:
    """Зафиксировать, что редактор намеренно выбрал не все площадки."""
    selected_set = set(selected)
    completed = _completed_platforms(session, post_id)
    for platform in _targets():
        if platform not in selected_set and platform not in completed:
            session.add(
                Publication(
                    post_id=post_id,
                    platform=platform,
                    status=PublishStatus.SKIPPED,
                    error="not selected by moderator",
                )
            )


def _requeue_incomplete_publications(session: Session) -> int:
    """Вернуть в очередь старые частично опубликованные посты.

    До появления пер-платформенных повторов статус PUBLISHED ставился после первого
    успешного канала. Эта проверка безопасно подхватывает такие записи: успешная
    площадка повторно не вызывается, а недостающая будет доставлена на следующей стадии.
    """
    if not _targets():
        return 0
    rows = session.scalars(
        select(RewrittenPost)
        .join(RawNews, RewrittenPost.raw_id == RawNews.id)
        .where(RawNews.status == NewsStatus.PUBLISHED)
    ).all()
    restored = 0
    for rp in rows:
        if _pending_targets(session, rp.id):
            rp.raw.status = NewsStatus.APPROVED
            restored += 1
            log.warning("partial_publication_requeued", post_id=rp.id)
    return restored


async def publish_post(
    session: Session,
    rp: RewrittenPost,
    selected_targets: list[str] | None = None,
) -> bool:
    """Доставить пост на все ещё неуспешные площадки.

    True означает, что пост есть на КАЖДОЙ активной площадке. Успешные каналы не
    вызываются повторно, поэтому повторная попытка после частичного сбоя не создаёт дубль.
    """
    targets = _targets()
    if not targets:
        log.error("publish_no_targets", post_id=rp.id)
        return False
    if selected_targets is not None:
        selected = [platform for platform in selected_targets if platform in targets]
        if not selected:
            log.error("publish_no_selected_targets", post_id=rp.id, requested=selected_targets)
            return False
        _skip_unselected_targets(session, rp.id, selected)

    pending = _pending_targets(session, rp.id)
    if not pending:
        if rp.raw:
            rp.raw.status = NewsStatus.PUBLISHED
        return True

    post = _to_post(rp)
    successful = set(targets) - set(pending)
    for platform in pending:
        publisher = get_publisher(platform)
        result = await publisher.publish(post)
        status = (
            PublishStatus.SUCCESS
            if result.ok
            else (PublishStatus.RATE_LIMITED if result.rate_limited else PublishStatus.FAILED)
        )
        session.add(
            Publication(
                post_id=rp.id,
                platform=platform,
                status=status,
                external_id=result.external_id,
                error=result.error,
                published_at=datetime.now(timezone.utc) if result.ok else None,
            )
        )
        if result.ok:
            successful.add(platform)
        log.info("published", post_id=rp.id, platform=platform, ok=result.ok, error=result.error)
    complete = set(targets).issubset(successful)
    if complete and rp.raw:
        rp.raw.status = NewsStatus.PUBLISHED
    return complete


def publish_post_id_sync(post_id: int, selected_targets: list[str] | None = None) -> bool:
    """Синхронная обёртка для вызова из бота-модератора (кнопка «Опубликовать»)."""
    with session_scope() as session:
        rp = session.get(RewrittenPost, post_id)
        if not rp:
            return False
        return asyncio.run(publish_post(session, rp, selected_targets))


def run_publish_stage(limit: int = 10) -> dict[str, int]:
    """Опубликовать одобренные посты и повторить только недоставленные площадки."""
    stats = {"published": 0, "skipped": 0, "failed": 0, "requeued_partial": 0}
    with session_scope() as session:
        stats["requeued_partial"] = _requeue_incomplete_publications(session)
        rows = session.scalars(
            select(RewrittenPost)
            .join(RawNews, RewrittenPost.raw_id == RawNews.id)
            .where(RawNews.status == NewsStatus.APPROVED)
            .order_by(RewrittenPost.created_at)
            .limit(limit)
        ).all()
        for rp in rows:
            retrying = bool(_successful_platforms(session, rp.id))
            ok_to_publish, why = can_publish_now(session, retrying=retrying)
            if not ok_to_publish:
                stats["skipped"] += 1
                log.info("publish_skipped", post_id=rp.id, reason=why)
                break  # лимит/интервал — остальные тоже подождут
            ok = asyncio.run(publish_post(session, rp))
            session.flush()
            stats["published" if ok else "failed"] += 1
    log.info("publish_stage_done", **stats)
    return stats
