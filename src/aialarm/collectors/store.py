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
from aialarm.media import MAX_IMAGES_PER_POST, raw_image_refs
from aialarm.source_policy import source_matches, visual_allowed

log = get_logger(__name__)


def _item_image_urls(item: CollectedItem) -> list[str]:
    candidates = list(item.image_urls or []) + ([item.image_url] if item.image_url else [])
    urls: list[str] = []
    for url in candidates:
        if url and url not in urls:
            urls.append(url)
    return urls[:MAX_IMAGES_PER_POST]


def _download_item_images(item: CollectedItem, key: str) -> list[str]:
    if not visual_allowed(item.source_url):
        return []
    refs: list[str] = []
    for index, url in enumerate(_item_image_urls(item)):
        image_key = f"{key[:24]}-{index:02d}"
        ref = download_and_store(url, image_key)
        if ref:
            refs.append(ref)
    return refs


def _backfill_images(raw: RawNews, item: CollectedItem) -> None:
    expected = _item_image_urls(item)
    if not expected or len(raw_image_refs(raw)) >= len(expected):
        return
    refs = _download_item_images(item, raw.dedup_key)
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
            dup_id, score = find_semantic_duplicate(session, emb, threshold)

            # Качаем весь альбом сразу: ссылки превью t.me быстро истекают.
            image_refs = _download_item_images(item, key)

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
    _backfill_images(original, item)
