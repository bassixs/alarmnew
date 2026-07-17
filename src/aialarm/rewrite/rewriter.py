"""Рерайт новости в стиль канала через LLM (structured output).

Промт следует ТЗ: пересказ фактов своими словами, без копирования структуры
предложений источника, сохранение фактов/цифр/имён, обязательная атрибуция источника,
без кликбейта. Атрибуция и (при необходимости) маркировка ИИ добавляются кодом —
не полагаемся на то, что модель их не забудет.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from aialarm.config import get_settings
from aialarm.db import session_scope
from aialarm.db.models import NewsStatus, RawNews, RewrittenPost
from aialarm.llm.client import get_llm_client
from aialarm.logging import get_logger

log = get_logger(__name__)

_SCHEMA = {
    "type": "object",
    "properties": {
        "post_text": {"type": "string"},
        "suggested_image_prompt": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["post_text", "hashtags"],
}

_SYSTEM = """Ты — редактор Telegram/MAX-канала «{channel}».
Стиль канала (tone of voice): {tone}
{examples}

Перепиши новость своими словами в стиле канала. Требования:
- Не копируй формулировки и структуру предложений источника — перескажи факты своими словами.
- Длина: {post_length}.
- Сохрани все факты, цифры, даты, имена — без искажений. Не добавляй фактов, которых нет в исходнике.
- Разбей на короткие абзацы; при уместности — 1-2 эмодзи в стиле канала; без канцелярита.
- Не используй кликбейт и не искажай тональность источника.
- НЕ добавляй сам подпись об источнике и хэштеги в конец текста — их подставит система.
Ответь строго по схеме инструмента (post_text, suggested_image_prompt, hashtags)."""


def _build_system() -> str:
    proj = get_settings().project
    examples = ""
    ex = [e for e in proj.tone_examples if e.strip()]
    if ex:
        examples = "Характерные примеры постов канала:\n" + "\n---\n".join(ex)
    return _SYSTEM.format(
        channel=proj.project_name,
        tone=proj.tone_of_voice or "нейтральный, человеческий",
        examples=examples,
        post_length=proj.post_length,
    )


def _attribution(source_url: str, has_image: bool) -> str:
    """Строка источника. По требованию: указывается ТОЛЬКО когда есть фото
    (фото берём у источника — кредитуем его). Без фото строки нет."""
    proj = get_settings().project
    parts: list[str] = []
    if has_image:
        parts.append(f"— источник {_domain(source_url)}")
    if proj.publish.ai_disclosure.strip():
        parts.append(proj.publish.ai_disclosure.strip())
    return "\n".join(parts)


def _domain(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    net = parsed.netloc
    if net in ("t.me", "telegram.me"):
        seg = parsed.path.strip("/").split("/")[0]
        return f"@{seg}" if seg else net  # Telegram-источник -> @канал
    return net or url


def rewrite_one(session: Session, raw: RawNews) -> RewrittenPost:
    system = _build_system()
    user = f'Новость: "{raw.title}. {(raw.body or "")[:4000]}"'
    llm = get_settings().project.llm
    data = get_llm_client().structured(
        model=llm.rewrite_model,
        system=system,
        user=user,
        schema=_SCHEMA,
        max_tokens=llm.max_tokens,
        temperature=llm.temperature,
    )
    post_text = str(data.get("post_text", "")).strip()
    # Строка источника (только если есть фото). Подвал площадки добавляется при публикации.
    attribution = _attribution(raw.source_url, has_image=bool(raw.image_url))
    if attribution:
        post_text = f"{post_text}\n\n{attribution}"

    post = RewrittenPost(
        raw_id=raw.id,
        post_text=post_text,
        suggested_image_prompt=str(data.get("suggested_image_prompt", "")),
        hashtags=list(data.get("hashtags", []) or []),
        model=llm.rewrite_model,
    )
    session.add(post)
    raw.status = NewsStatus.REWRITTEN
    log.info("rewritten", raw_id=raw.id, length=len(post_text))
    return post


def run_rewrite_stage(limit: int = 20) -> dict[str, int]:
    stats = {"rewritten": 0, "errors": 0}
    with session_scope() as session:
        rows = session.scalars(
            select(RawNews).where(RawNews.status == NewsStatus.RELEVANT).limit(limit)
        ).all()
        for raw in rows:
            try:
                rewrite_one(session, raw)
                stats["rewritten"] += 1
            except Exception as e:  # noqa: BLE001
                raw.status = NewsStatus.ERROR
                stats["errors"] += 1
                log.error("rewrite_failed", raw_id=raw.id, error=str(e))
    log.info("rewrite_stage_done", **stats)
    return stats
