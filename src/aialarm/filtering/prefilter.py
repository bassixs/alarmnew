"""Дешёвый семантический префильтр: близость новости к тезисам канала.

Эмбеддинги тезисов кэшируются. Возвращаем максимальную близость по всем тезисам —
если она ниже порога, LLM не вызываем (экономия). Trade-off: на TF-IDF fallback
префильтр грубее, поэтому порог по умолчанию мягкий (0.35), чтобы не терять кандидатов.
"""
from __future__ import annotations

from functools import lru_cache

from aialarm.config import get_settings
from aialarm.llm.embeddings import cosine, get_embedder


@lru_cache(maxsize=1)
def _thesis_embeddings() -> list[list[float]]:
    theses = get_settings().project.theses
    if not theses:
        return []
    return get_embedder().embed_many(theses)


def prefilter_score(title: str, body: str) -> float:
    theses_emb = _thesis_embeddings()
    if not theses_emb:
        return 1.0  # тезисы не заданы — не режем на префильтре
    text = f"{title}. {(body or '')[:600]}"
    emb = get_embedder().embed(text)
    return max(cosine(emb, t) for t in theses_emb)


def invalidate_cache() -> None:
    _thesis_embeddings.cache_clear()
