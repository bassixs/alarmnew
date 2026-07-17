"""Дедупликация новостей.

Два уровня:
1. Точный: sha256(url|title) — уникальный индекс в БД, отсекает повторный сбор.
2. Семантический: косинусная близость эмбеддинга (заголовок+первый абзац) с недавно
   собранными новостями. Близость > threshold -> дубль из другого источника.

Пилот держит эмбеддинги в JSON и сравнивает в памяти по свежему окну (это ок при
десятках-сотнях новостей в сутки). Прод: колонка pgvector + индекс ivfflat/hnsw и
`ORDER BY embedding <=> :q LIMIT k` на стороне БД — тот же интерфейс, другая реализация.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from aialarm.db.models import NewsStatus, RawNews
from aialarm.llm.embeddings import cosine


def dedup_text(title: str, body: str) -> str:
    first_para = (body or "").strip().split("\n\n", 1)[0]
    return f"{title}. {first_para}".strip()


def find_semantic_duplicate(
    session: Session,
    embedding: list[float],
    threshold: float,
    lookback_hours: int = 48,
) -> tuple[int | None, float]:
    """Вернуть (id новости-оригинала, score) или (None, best_score)."""
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    stmt = (
        select(RawNews)
        .where(RawNews.collected_at >= since)
        .where(RawNews.embedding.is_not(None))
        .where(RawNews.status != NewsStatus.DUPLICATE)
        .order_by(RawNews.id.desc())
        .limit(500)
    )
    best_id, best_score = None, 0.0
    for row in session.scalars(stmt):
        score = cosine(embedding, row.embedding)  # type: ignore[arg-type]
        if score > best_score:
            best_id, best_score = row.id, score
    if best_score >= threshold:
        return best_id, best_score
    return None, best_score
