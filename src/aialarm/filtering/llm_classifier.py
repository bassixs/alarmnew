"""LLM-классификатор релевантности (Вариант A из ТЗ)."""
from __future__ import annotations

from dataclasses import dataclass

from aialarm.config import get_settings
from aialarm.llm.client import get_llm_client

_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "matched_thesis": {"type": "string"},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string"},
    },
    "required": ["relevant", "matched_thesis", "confidence", "reason"],
}

_SYSTEM = """Ты — редактор регионального новостного канала.
Канал и аудитория: {tone}
Тезисы, под которые ДОЛЖНА подходить новость (иначе не публикуем):
{theses}

Твоя задача — решить, релевантна ли новость хотя бы одному тезису.
Будь строгим и отдавай предпочтение значимым, официальным и полезным новостям;
проходные и малозначимые заметки оценивай низкой уверенностью.
Считай НЕРЕЛЕВАНТНЫМИ: рекламу и промо, гороскопы, розыгрыши и конкурсы репостов,
частные объявления, а также федеральные/иногородние новости без прямой связи с
Калужской областью.
confidence отражает и релевантность, и значимость новости для канала.
Ответь строго по схеме инструмента."""


@dataclass(slots=True)
class ClassifyResult:
    relevant: bool
    matched_thesis: str
    confidence: int
    reason: str


def classify(title: str, body: str) -> ClassifyResult:
    proj = get_settings().project
    theses = "\n".join(f"- {t}" for t in proj.theses)
    system = _SYSTEM.format(tone=proj.tone_of_voice or "региональные новости", theses=theses)
    user = f'Новость: "{title}. {(body or "")[:2000]}"'
    data = get_llm_client().structured(
        model=proj.llm.classify_model,
        system=system,
        user=user,
        schema=_SCHEMA,
        max_tokens=400,
    )
    return ClassifyResult(
        relevant=bool(data.get("relevant")),
        matched_thesis=str(data.get("matched_thesis", "")),
        confidence=int(data.get("confidence", 0)),
        reason=str(data.get("reason", "")),
    )
