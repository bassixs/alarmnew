"""Оркестрация фильтрации: правила -> префильтр -> LLM. Пишет filtered_news и статус.

Связка B -> A из ТЗ: дешёвый эмбеддинг-префильтр отсекает явный мусор, LLM выносит
финальное решение только по кандидатам. exclude/sensitive не роняются молча —
помечаются is_sensitive и уходят в обязательную модерацию (см. moderation).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from aialarm.config import get_settings
from aialarm.db import session_scope
from aialarm.db.models import FilteredNews, NewsStatus, RawNews
from aialarm.filtering.llm_classifier import classify
from aialarm.filtering.prefilter import prefilter_score
from aialarm.filtering.rules import check_rules
from aialarm.logging import get_logger

log = get_logger(__name__)


def filter_one(session: Session, raw: RawNews) -> FilteredNews:
    proj = get_settings().project
    fcfg = proj.filter

    rule = check_rules(raw.title, raw.body, proj.exclude_keywords, proj.sensitive_keywords)
    is_sensitive = rule.sensitive or rule.excluded

    score = prefilter_score(raw.title, raw.body)
    relevant, matched, confidence, reason, decided_by = False, "", 0, "", "prefilter"

    prefilter_passed = score >= fcfg.embed_relevance_min

    if fcfg.strategy == "prefilter_only":
        relevant = prefilter_passed
        confidence = int(score * 100)
        matched = "(prefilter)"
        reason = f"semantic score={score:.2f}"
    elif fcfg.strategy == "llm_only" or (fcfg.strategy == "prefilter_then_llm" and prefilter_passed):
        res = classify(raw.title, raw.body)
        relevant, matched, confidence, reason = (
            res.relevant, res.matched_thesis, res.confidence, res.reason,
        )
        decided_by = "llm"
    else:  # prefilter_then_llm и префильтр не прошёл
        reason = f"prefilter score={score:.2f} < {fcfg.embed_relevance_min}"

    # Чувствительные новости (криминал/ДТП/суд/ЧП/exclude по ключевым словам) ВСЕГДА идут
    # на модерацию — даже если LLM счёл их нерелевантными: редактор решает сам, не роняем
    # молча. Обычные новости проходят при релевантности и уверенности выше порога.
    passes = is_sensitive or (relevant and confidence >= fcfg.llm_confidence_min)

    filtered = FilteredNews(
        raw_id=raw.id,
        relevant=relevant,
        matched_thesis=matched,
        confidence=confidence,
        reason=reason,
        is_sensitive=is_sensitive,
        prefilter_score=score,
        decided_by=decided_by,
    )
    session.add(filtered)

    if passes:
        raw.status = NewsStatus.RELEVANT
    elif rule.excluded:
        raw.status = NewsStatus.EXCLUDED
    else:
        raw.status = NewsStatus.FILTERED_OUT

    log.info(
        "filtered",
        raw_id=raw.id,
        status=raw.status.value,
        relevant=relevant,
        confidence=confidence,
        sensitive=is_sensitive,
        score=round(score, 3),
    )
    return filtered


def run_filter_stage(limit: int = 50) -> dict[str, int]:
    """Обработать все новости в статусе NEW."""
    stats = {"processed": 0, "relevant": 0, "filtered_out": 0, "excluded": 0}
    with session_scope() as session:
        rows = session.scalars(
            select(RawNews).where(RawNews.status == NewsStatus.NEW).limit(limit)
        ).all()
        for raw in rows:
            filter_one(session, raw)
            stats["processed"] += 1
            if raw.status == NewsStatus.RELEVANT:
                stats["relevant"] += 1
            elif raw.status == NewsStatus.EXCLUDED:
                stats["excluded"] += 1
            else:
                stats["filtered_out"] += 1
    log.info("filter_stage_done", **stats)
    return stats
