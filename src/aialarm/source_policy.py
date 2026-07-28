"""Политики использования визуалов для отдельных источников."""
from __future__ import annotations

from urllib.parse import urlparse

from aialarm.config import get_settings

_DEFAULT_WARNING = "изображения этого источника запрещено использовать"


def _telegram_channel(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if "://" not in value:
        return value.lstrip("@").strip("/").lower()
    parsed = urlparse(value)
    if parsed.netloc.lower() not in {"t.me", "telegram.me", "www.t.me"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0].lower() == "s":
        parts.pop(0)
    return parts[0].lstrip("@").lower() if parts else None


def source_matches(configured_url: str, candidate_url: str) -> bool:
    """Сопоставить URL источника из конфига со ссылкой на конкретную публикацию."""
    configured_channel = _telegram_channel(configured_url)
    candidate_channel = _telegram_channel(candidate_url)
    if configured_channel and candidate_channel:
        return configured_channel == candidate_channel
    configured = configured_url.strip().rstrip("/").lower()
    candidate = candidate_url.strip().rstrip("/").lower()
    return bool(configured) and (candidate == configured or candidate.startswith(configured + "/"))


def visual_policy(source_url: str) -> tuple[bool, str]:
    for source in get_settings().project.sources:
        if source_matches(source.url, source_url):
            if source.allow_visual:
                return True, ""
            return False, source.visual_warning.strip() or _DEFAULT_WARNING
    return True, ""


def visual_allowed(source_url: str) -> bool:
    return visual_policy(source_url)[0]
