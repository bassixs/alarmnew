"""Сохранение собранных новостей в raw_news с дедупликацией."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from aialarm.collectors.base import CollectedItem
from aialarm.collectors.dedup import dedup_text, find_semantic_duplicate
from aialarm.collectors.images import cached_image_path, download_images
from aialarm.config import get_settings
from aialarm.db import session_scope
from aialarm.db.models import NewsStatus, RawNews
from aialarm.llm.embeddings import get_embedder
from aialarm.logging import get_logger
from aialarm.media import MAX_IMAGES_PER_POST, raw_image_refs
from aialarm.source_policy import source_matches, visual_allowed

log = get_logger(__name__)
_ACTIVE_MEDIA_STATUSES = {
    NewsStatus.NEW, NewsStatus.RELEVANT, NewsStatus.PREVIEW,
    NewsStatus.REWRITTEN, NewsStatus.MODERATION, NewsStatus.APPROVED,
}


def _item_image_urls(item: CollectedItem) -> list[str]:
    candidates = list(item.image_urls or []) + ([item.image_url] if item.image_url else [])
    urls: list[str] = []
    for url in candidates:
        if url and url not in urls:
            urls.append(url)
    return urls[:MAX_IMAGES_PER_POST]


def _cached_item_images(item: CollectedItem, key: str) -> list[str]:
    if not visual_allowed(item.source_url):
        return []
    refs: list[str] = []
    for index, url in enumerate(_item_image_urls(item)):
        image_key = f"{key[:24]}-{index:02d}"
        ref = cached_image_path(image_key)
        if ref:
            refs.append(ref)
    return refs


def _backfill_images(raw: RawNews, item: CollectedItem) -> None:
    if raw.status not in _ACTIVE_MEDIA_STATUSES:
        return
    expected = _item_image_urls(item)
    existing = [ref for ref in raw_image_refs(raw) if Path(ref).is_file()]
    if not expected or len(existing) >= len(expected):
        return
    refs = list(dict.fromkeys(existing + _cached_item_images(item, raw.dedup_key)))[:MAX_IMAGES_PER_POST]
    if refs:
        raw.image_urls = refs
        raw.image_url = refs[0]


def _is_district_source(item: CollectedItem) -> bool:
    return any(
        source.district_id and source_matches(source.url, item.item_url or item.source_url)
        for source in get_settings().project.sources
    )


def store_items(items: list[CollectedItem]) -> dict[str, int]:
    """Записать новые новости. Возвращает счётчики для мониторинга."""
    cfg = get_settings().project
    embedder = get_embedder()
    threshold = cfg.filter.dedup_cosine_threshold
    stats = {"seen": len(items), "inserted": 0, "exact_dup": 0, "semantic_dup": 0}

    with session_scope() as session:
        for item in items:
            key = item.dedup_key()
            existing = session.scalar(select(RawNews).where(RawNews.dedup_key == key))
            if existing:
                _backfill_images(existing, item)
                stats["exact_dup"] += 1
                continue

            emb = embedder.embed(dedup_text(item.title, item.body))
            dup_id, _ = find_semantic_duplicate(session, emb, threshold)

            # Text must commit even if Telegram's image CDN is unavailable.
            # Networking happens later in cache_collected_images, outside this transaction.
            image_refs = _cached_item_images(item, key) if dup_id is None else []

            row = RawNews(
                dedup_key=key,
                source_type=item.source_type,
                source_url=item.item_url or item.source_url,
                region=item.region,
                is_district_source=_is_district_source(item),
                title=item.title,
                body=item.body,
                image_url=image_refs[0] if image_refs else None,
                image_urls=image_refs,
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
    if not original:
        return
    if len(item.body or "") > len(original.body or ""):
        original.body = item.body


async def cache_collected_images(items: list[CollectedItem]) -> int:
    """Download only active cards' missing media, after committing every source."""
    by_key = {item.dedup_key(): item for item in items if _item_image_urls(item)}
    if not by_key:
        return 0
    requests: dict[str, str] = {}
    candidates: dict[int, CollectedItem] = {}
    with session_scope() as session:
        rows = session.scalars(select(RawNews).where(
            RawNews.dedup_key.in_(by_key), RawNews.status.in_(_ACTIVE_MEDIA_STATUSES),
        )).all()
        for raw in rows:
            item = by_key[raw.dedup_key]
            if not visual_allowed(raw.source_url):
                continue
            expected = _item_image_urls(item)
            existing = [ref for ref in raw_image_refs(raw) if Path(ref).is_file()]
            if len(existing) >= len(expected):
                continue
            candidates[raw.id] = item
            for index, url in enumerate(expected):
                requests[f"{raw.dedup_key[:24]}-{index:02d}"] = url
    saved = await download_images(requests)
    with session_scope() as session:
        for raw_id, item in candidates.items():
            raw = session.get(RawNews, raw_id)
            if raw:
                _backfill_images(raw, item)
    return len(saved)
