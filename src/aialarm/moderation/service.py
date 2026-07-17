"""Логика модерации: маршрутизация после рерайта и операции approve/reject/edit.

Маршрутизация (по config.moderation.mode):
- off           -> пост сразу APPROVED (публикуется автоматически);
- all           -> всё уходит на модерацию;
- sensitive_only-> на модерацию только is_sensitive, остальное APPROVED.

Чувствительные/exclude-новости ВСЕГДА идут на модерацию независимо от mode
(is_sensitive), т.к. по ТЗ их нельзя публиковать автоматически.

Отправка карточки модератору вынесена в notify.py (короткоживущий Bot), а обработка
нажатий кнопок — в bot.py (long-running polling).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from aialarm.config import get_settings
from aialarm.db import session_scope
from aialarm.db.models import FilteredNews, NewsStatus, RawNews, RewrittenPost
from aialarm.logging import get_logger

log = get_logger(__name__)


def _needs_moderation(session: Session, raw: RawNews) -> bool:
    mod = get_settings().project.moderation
    if not mod.enabled or mod.mode == "off":
        # Даже при выключенной модерации чувствительное не публикуем автоматически.
        return _is_sensitive(session, raw.id)
    if mod.mode == "all":
        return True
    if mod.mode == "sensitive_only":
        return _is_sensitive(session, raw.id)
    return True


def _is_sensitive(session: Session, raw_id: int) -> bool:
    fn = session.scalar(select(FilteredNews).where(FilteredNews.raw_id == raw_id))
    return bool(fn and fn.is_sensitive)


def route_previews(limit: int = 50) -> dict[str, int]:
    """RELEVANT -> PREVIEW: шлём модератору ОРИГИНАЛ (без рерайта) с кнопками
    «Переписать»/«Отменить». Рерайт (Sonnet) откладывается до нажатия «Переписать» —
    не тратим деньги на посты, которые не возьмут."""
    from aialarm.moderation.notify import send_preview

    stats = {"to_preview": 0, "auto_approved": 0}
    to_notify: list[int] = []
    with session_scope() as session:
        rows = session.scalars(
            select(RawNews).where(RawNews.status == NewsStatus.RELEVANT).limit(limit)
        ).all()
        for raw in rows:
            if _needs_moderation(session, raw):
                raw.status = NewsStatus.PREVIEW
                stats["to_preview"] += 1
                to_notify.append(raw.id)
            else:
                # Автопубликация без модерации: переписываем сразу и одобряем.
                from aialarm.rewrite.rewriter import rewrite_one

                rewrite_one(session, raw)
                raw.status = NewsStatus.APPROVED
                stats["auto_approved"] += 1

    # Throttle ~4с между карточками: лимит мессенджеров ~20 сообщений/мин в один чат.
    import time

    for i, raw_id in enumerate(to_notify):
        if i:
            time.sleep(4)
        try:
            send_preview(raw_id)
        except Exception as e:  # noqa: BLE001
            log.error("preview_notify_failed", raw_id=raw_id, error=str(e))

    log.info("preview_routing_done", **stats)
    return stats


def rewrite_and_get(raw_id: int) -> int | None:
    """По нажатию «Переписать»: переписываем оригинал (Sonnet), возвращаем post_id
    готового поста. Если уже переписан — возвращаем существующий (без повтора)."""
    from aialarm.rewrite.rewriter import rewrite_one

    done = {NewsStatus.REWRITTEN, NewsStatus.MODERATION, NewsStatus.APPROVED, NewsStatus.PUBLISHED}
    with session_scope() as session:
        raw = session.get(RawNews, raw_id)
        if not raw:
            return None
        existing = session.scalar(select(RewrittenPost).where(RewrittenPost.raw_id == raw_id))
        if existing and raw.status in done:
            return existing.id
        rp = existing or rewrite_one(session, raw)
        raw.status = NewsStatus.MODERATION  # готовый пост ждёт опубликовать/править/отклонить
        session.flush()
        log.info("preview_rewritten", raw_id=raw_id, post_id=rp.id)
        return rp.id


def cancel_preview(raw_id: int) -> bool:
    """По нажатию «Отменить»: отклоняем новость (сообщение удаляет бот)."""
    with session_scope() as session:
        raw = session.get(RawNews, raw_id)
        if not raw:
            return False
        raw.status = NewsStatus.REJECTED
        log.info("preview_cancelled", raw_id=raw_id)
        return True


def get_preview(raw_id: int) -> dict | None:
    """Данные карточки-оригинала для модератора."""
    with session_scope() as session:
        raw = session.get(RawNews, raw_id)
        if not raw:
            return None
        fn = session.scalar(select(FilteredNews).where(FilteredNews.raw_id == raw_id))
        return {
            "raw_id": raw.id,
            "title": raw.title,
            "body": raw.body,
            "source_url": raw.source_url,
            "confidence": fn.confidence if fn else 0,
            "matched_thesis": fn.matched_thesis if fn else "",
            "is_sensitive": fn.is_sensitive if fn else False,
            "has_image": bool(raw.image_url),
        }


# ── Операции, вызываемые из бота ─────────────────────────────────────────────
def approve(post_id: int) -> bool:
    with session_scope() as session:
        rp = session.get(RewrittenPost, post_id)
        if not rp or not rp.raw:
            return False
        if rp.raw.status in (NewsStatus.PUBLISHED, NewsStatus.REJECTED):
            return False  # уже обработано — не публикуем повторно
        rp.raw.status = NewsStatus.APPROVED
        log.info("moderation_approved", post_id=post_id)
        return True


def reject(post_id: int) -> bool:
    with session_scope() as session:
        rp = session.get(RewrittenPost, post_id)
        if not rp or not rp.raw:
            return False
        rp.raw.status = NewsStatus.REJECTED
        log.info("moderation_rejected", post_id=post_id)
        return True


def apply_edit(post_id: int, new_text: str) -> bool:
    with session_scope() as session:
        rp = session.get(RewrittenPost, post_id)
        if not rp or not rp.raw:
            return False
        rp.post_text = new_text
        rp.edited_by_moderator = True
        rp.raw.status = NewsStatus.APPROVED
        log.info("moderation_edited", post_id=post_id)
        return True


def get_pending(post_id: int) -> dict | None:
    """Данные карточки для отображения модератору."""
    with session_scope() as session:
        rp = session.get(RewrittenPost, post_id)
        if not rp or not rp.raw:
            return None
        fn = session.scalar(select(FilteredNews).where(FilteredNews.raw_id == rp.raw_id))
        return {
            "post_id": rp.id,
            "post_text": rp.post_text,
            "source_url": rp.raw.source_url,
            "title": rp.raw.title,
            "confidence": fn.confidence if fn else 0,
            "matched_thesis": fn.matched_thesis if fn else "",
            "is_sensitive": fn.is_sensitive if fn else False,
        }
