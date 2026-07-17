"""Метрики воронки для калибровки тезисов/промтов и еженедельного отчёта.

увидено -> прошло фильтр -> опубликовано -> отклонено модератором.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from aialarm.db import session_scope
from aialarm.db.models import (
    FilteredNews,
    NewsStatus,
    Publication,
    PublishStatus,
    RawNews,
    RewrittenPost,
)


def funnel(days: int = 7) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    with session_scope() as s:
        seen = s.scalar(select(func.count(RawNews.id)).where(RawNews.collected_at >= since)) or 0
        dups = s.scalar(
            select(func.count(RawNews.id))
            .where(RawNews.collected_at >= since)
            .where(RawNews.status == NewsStatus.DUPLICATE)
        ) or 0
        relevant = s.scalar(
            select(func.count(FilteredNews.id))
            .join(RawNews, FilteredNews.raw_id == RawNews.id)
            .where(RawNews.collected_at >= since)
            .where(FilteredNews.relevant.is_(True))
        ) or 0
        rewritten = s.scalar(
            select(func.count(RewrittenPost.id))
            .join(RawNews, RewrittenPost.raw_id == RawNews.id)
            .where(RawNews.collected_at >= since)
        ) or 0
        rejected = s.scalar(
            select(func.count(RawNews.id))
            .where(RawNews.collected_at >= since)
            .where(RawNews.status == NewsStatus.REJECTED)
        ) or 0
        edited = s.scalar(
            select(func.count(RewrittenPost.id))
            .join(RawNews, RewrittenPost.raw_id == RawNews.id)
            .where(RawNews.collected_at >= since)
            .where(RewrittenPost.edited_by_moderator.is_(True))
        ) or 0
        published = s.scalar(
            select(func.count(func.distinct(Publication.post_id)))
            .where(Publication.status == PublishStatus.SUCCESS)
            .where(Publication.published_at >= since)
        ) or 0

        approved_no_edit = published - edited
        return {
            "period_days": days,
            "seen": seen,
            "duplicates": dups,
            "relevant": relevant,
            "rewritten": rewritten,
            "published": published,
            "rejected_by_moderator": rejected,
            "edited_by_moderator": edited,
            "published_without_edits": max(0, approved_no_edit),
            # Метрики качества фильтра/промта (см. ТЗ, п.6):
            "clean_pass_rate": round(approved_no_edit / published, 2) if published else None,
            "reject_rate": round(rejected / rewritten, 2) if rewritten else None,
        }
