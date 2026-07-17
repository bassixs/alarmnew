"""Сохранение собранных новостей в raw_news с дедупликацией."""
from __future__ import annotations

from sqlalchemy import select

from aialarm.collectors.base import CollectedItem
from aialarm.collectors.dedup import dedup_text, find_semantic_duplicate
from aialarm.collectors.images import download_and_store
from aialarm.config import get_settings
from aialarm.db import session_scope
from aialarm.db.models import NewsStatus, RawNews
from aialarm.llm.embeddings import get_embedder
from aialarm.logging import get_logger

log = get_logger(__name__)


def store_items(items: list[CollectedItem]) -> dict[str, int]:
    """Записать новые новости. Возвращает счётчики для мониторинга."""
    cfg = get_settings().project
    embedder = get_embedder()
    threshold = cfg.filter.dedup_cosine_threshold
    stats = {"seen": len(items), "inserted": 0, "exact_dup": 0, "semantic_dup": 0}

    with session_scope() as session:
        for item in items:
            key = item.dedup_key()
            exists = session.scalar(select(RawNews.id).where(RawNews.dedup_key == key))
            if exists:
                stats["exact_dup"] += 1
                continue

            emb = embedder.embed(dedup_text(item.title, item.body))
            dup_id, score = find_semantic_duplicate(session, emb, threshold)

            # Качаем фото сразу (ссылки превью t.me быстро истекают) -> локальный путь.
            image_ref = download_and_store(item.image_url, key) if item.image_url else None

            row = RawNews(
                dedup_key=key,
                source_type=item.source_type,
                source_url=item.item_url or item.source_url,
                region=item.region,
                title=item.title,
                body=item.body,
                image_url=image_ref,
                published_at=item.published_at,
                embedding=emb,
            )
            if dup_id is not None:
                row.status = NewsStatus.DUPLICATE
                row.duplicate_of = dup_id
                stats["semantic_dup"] += 1
                # Если новый источник даёт более полный текст — обновим оригинал.
                _maybe_enrich_original(session, dup_id, item)
            else:
                row.status = NewsStatus.NEW
                stats["inserted"] += 1
            session.add(row)

    log.info("store_items", **stats)
    return stats


def _maybe_enrich_original(session, original_id: int, item: CollectedItem) -> None:
    original = session.get(RawNews, original_id)
    if original and len(item.body or "") > len(original.body or ""):
        original.body = item.body
        if item.image_url and not original.image_url:
            original.image_url = item.image_url
