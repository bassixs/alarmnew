"""Общая политика фотоальбомов для модерации и публикации."""
from __future__ import annotations

from aialarm.source_policy import visual_allowed

MAX_IMAGES_PER_POST = 10


def raw_image_refs(raw) -> list[str]:  # noqa: ANN001
    """Вернуть разрешённые локальные изображения, включая старое одиночное поле."""
    if raw is None or not visual_allowed(raw.source_url):
        return []
    refs: list[str] = []
    for ref in list(raw.image_urls or []) + ([raw.image_url] if raw.image_url else []):
        if ref and ref not in refs:
            refs.append(ref)
    return refs[:MAX_IMAGES_PER_POST]
