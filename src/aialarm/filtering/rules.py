"""Правила на стоп-словах и чувствительных темах.

Важно из ТЗ: exclude/sensitive НЕ отбрасывают новость молча — они переводят её в
ручную модерацию, чтобы редактор не терял важные инфоповоды.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RuleHit:
    excluded: bool
    sensitive: bool
    matched: list[str]


def _match(text: str, keywords: list[str]) -> list[str]:
    low = text.lower()
    return [kw for kw in keywords if kw.lower().strip() and kw.lower() in low]


def check_rules(title: str, body: str, exclude: list[str], sensitive: list[str]) -> RuleHit:
    text = f"{title}\n{body}"
    ex = _match(text, exclude)
    se = _match(text, sensitive)
    return RuleHit(excluded=bool(ex), sensitive=bool(se), matched=ex + se)
