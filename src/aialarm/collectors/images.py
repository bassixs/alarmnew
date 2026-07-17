"""Локальное сохранение картинок новостей.

Превью t.me (cdn telesco.pe) живут недолго — качаем сразу при сборе и храним файл,
чтобы к моменту публикации фото гарантированно было. Публикаторы затем берут локальный
файл: Telegram отправляет как файл, MAX — через upload.
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx

from aialarm.logging import get_logger

log = get_logger(__name__)

IMAGES_DIR = Path("data/images")
_MAX_BYTES = 9_500_000
_UA = "Mozilla/5.0 (compatible; aialarm/0.1)"


def is_local(path_or_url: str | None) -> bool:
    return bool(path_or_url) and not str(path_or_url).startswith(("http://", "https://"))


def download_and_store(url: str, key: str) -> str | None:
    """Скачать картинку и сохранить в data/images/<key>.jpg. Вернуть путь или None."""
    if not url or not url.startswith(("http://", "https://")):
        return None
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    dest = IMAGES_DIR / f"{key[:32]}.jpg"
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            r = client.get(url, headers={"User-Agent": _UA})
            r.raise_for_status()
            data = r.content
        if not data or len(data) > _MAX_BYTES:
            return None
        dest.write_bytes(data)
        return str(dest)
    except Exception as e:  # noqa: BLE001
        log.warning("image_download_failed", url=url[:80], error=str(e))
        return None


def cleanup_old(days: int = 2) -> int:
    """Удалить картинки старше N дней. Возвращает число удалённых."""
    if not IMAGES_DIR.exists():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for f in IMAGES_DIR.glob("*.jpg"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed
