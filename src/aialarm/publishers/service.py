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


def can_publish_now(session: Session) -> tuple[bool, str]:
    pub = get_settings().project.publish
    if _today_success_count(session) >= pub.max_posts_per_day:
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
    image = rp.raw.image_url if rp.raw else None
    return Post(text=rp.post_text, image_url=image, hashtags=list(rp.hashtags or []))


async def publish_post(session: Session, rp: RewrittenPost) -> bool:
    """Опубликовать один пост на все активные площадки. Возвращает True, если хоть куда-то ок."""
    targets = get_settings().project.publish.targets
    post = _to_post(rp)
    any_ok = False
    for platform in targets:
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
        any_ok = any_ok or result.ok
        log.info("published", post_id=rp.id, platform=platform, ok=result.ok, error=result.error)
    if any_ok and rp.raw:
        rp.raw.status = NewsStatus.PUBLISHED
    return any_ok


def publish_post_id_sync(post_id: int) -> bool:
    """Синхронная обёртка для вызова из бота-модератора (кнопка «Опубликовать»)."""
    with session_scope() as session:
        rp = session.get(RewrittenPost, post_id)
        if not rp:
            return False
        return asyncio.run(publish_post(session, rp))


def run_publish_stage(limit: int = 10) -> dict[str, int]:
    """Опубликовать одобренные посты, соблюдая лимиты частоты."""
    stats = {"published": 0, "skipped": 0, "failed": 0}
    with session_scope() as session:
        rows = session.scalars(
            select(RewrittenPost)
            .join(RawNews, RewrittenPost.raw_id == RawNews.id)
            .where(RawNews.status == NewsStatus.APPROVED)
            .order_by(RewrittenPost.created_at)
            .limit(limit)
        ).all()
        for rp in rows:
            ok_to_publish, why = can_publish_now(session)
            if not ok_to_publish:
                stats["skipped"] += 1
                log.info("publish_skipped", post_id=rp.id, reason=why)
                break  # лимит/интервал — остальные тоже подождут
            ok = asyncio.run(publish_post(session, rp))
            session.flush()
            stats["published" if ok else "failed"] += 1
    log.info("publish_stage_done", **stats)
    return stats
