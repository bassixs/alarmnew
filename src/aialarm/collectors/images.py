"""Локальное сохранение картинок новостей.

Превью t.me (cdn telesco.pe) живут недолго — кешируем при сборе, после сохранения
текстов и вне транзакций БД. Недоступные изображения не блокируют новости.
Публикаторы берут сохранённый локальный файл: Telegram отправляет как файл,
MAX — через upload.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from aialarm.logging import get_logger

log = get_logger(__name__)

IMAGES_DIR = Path("data/images")
_MAX_BYTES = 9_500_000
_UA = "Mozilla/5.0 (compatible; aialarm/0.1)"
_IMAGE_TIMEOUT_SECONDS = 8
_BATCH_TIMEOUT_SECONDS = 30
_MAX_CONCURRENT_DOWNLOADS = 8
_HOST_COOLDOWN_SECONDS = 120
_host_retry_after: dict[str, float] = {}


def is_local(path_or_url: str | None) -> bool:
    return bool(path_or_url) and not str(path_or_url).startswith(("http://", "https://"))


def cached_image_path(key: str) -> str | None:
    path = IMAGES_DIR / f"{key[:32]}.jpg"
    return str(path) if path.is_file() and path.stat().st_size else None


async def download_images(requests: dict[str, str]) -> dict[str, str]:
    """Cache images outside DB transactions, with hard image and batch deadlines.

    A failed CDN gets a short cooldown instead of delaying every image from the
    same host. Successful files are reused on the next collection pass.
    """
    if not requests:
        return {}
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    saved: dict[str, str] = {}
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DOWNLOADS)
    async with httpx.AsyncClient(timeout=httpx.Timeout(5, connect=3), follow_redirects=True,
                                 limits=httpx.Limits(max_connections=_MAX_CONCURRENT_DOWNLOADS)) as client:
        async def fetch(key: str, url: str) -> None:
            cached = cached_image_path(key)
            if cached:
                saved[key] = cached
                return
            if not url.startswith(("http://", "https://")):
                return
            host = urlsplit(url).netloc
            async with semaphore:
                if _host_retry_after.get(host, 0) > time.monotonic():
                    return
                try:
                    async with asyncio.timeout(_IMAGE_TIMEOUT_SECONDS):
                        async with client.stream("GET", url, headers={"User-Agent": _UA}) as response:
                            response.raise_for_status()
                            data = bytearray()
                            async for chunk in response.aiter_bytes():
                                data.extend(chunk)
                                if len(data) > _MAX_BYTES:
                                    return
                    if not data:
                        return
                    dest = IMAGES_DIR / f"{key[:32]}.jpg"
                    temporary = dest.with_suffix(f".{uuid4().hex}.tmp")
                    try:
                        temporary.write_bytes(data)
                        temporary.replace(dest)
                    finally:
                        temporary.unlink(missing_ok=True)
                    saved[key] = str(dest)
                except (httpx.HTTPError, TimeoutError, OSError) as exc:
                    # A missing individual image does not mean the CDN is down.
                    if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code >= 500:
                        _host_retry_after[host] = time.monotonic() + _HOST_COOLDOWN_SECONDS
                    log.warning("image_download_failed", host=host, error=type(exc).__name__)

        tasks = [asyncio.create_task(fetch(key, url)) for key, url in requests.items()]
        try:
            async with asyncio.timeout(_BATCH_TIMEOUT_SECONDS):
                await asyncio.gather(*tasks)
        except TimeoutError:
            log.warning("image_batch_timeout", requested=len(requests), saved=len(saved))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    log.info("image_cache_done", requested=len(requests), saved=len(saved))
    return saved


def cleanup_old(days: int = 2) -> int:
    """Удалить неиспользуемые картинки старше N дней.

    Файлы активной очереди сохраняем независимо от возраста: дежурный может вернуться
    к карточке позже, и публикация не должна внезапно потерять изображение.
    """
    if not IMAGES_DIR.exists():
        return 0
    try:
        from sqlalchemy import select

        from aialarm.db import session_scope
        from aialarm.db.models import NewsStatus, RawNews, RewrittenPost

        protected_statuses = (
            NewsStatus.NEW,
            NewsStatus.RELEVANT,
            NewsStatus.PREVIEW,
            NewsStatus.REWRITTEN,
            NewsStatus.MODERATION,
            NewsStatus.APPROVED,
        )
        with session_scope() as session:
            rows = session.execute(
                select(RawNews.image_url, RawNews.image_urls)
                .where(RawNews.status.in_(protected_statuses))
            ).all()
            generated_rows = session.execute(
                select(RewrittenPost.generated_image_path)
                .join(RawNews, RewrittenPost.raw_id == RawNews.id)
                .where(RawNews.status.in_(protected_statuses))
            ).scalars().all()
        refs: list[str] = []
        for primary, album in rows:
            refs.extend(ref for ref in list(album or []) if ref)
            if primary:
                refs.append(primary)
        refs.extend(ref for ref in generated_rows if ref)
        protected = {str(Path(ref)).replace("\\", "/") for ref in refs}
    except Exception as e:  # noqa: BLE001
        # При проблеме с БД безопаснее пропустить очистку, чем удалить активное фото.
        log.warning("image_cleanup_skipped", error=str(e))
        return 0

    cutoff = time.time() - days * 86400
    removed = 0
    for f in IMAGES_DIR.rglob("*.jpg"):
        try:
            normalized = str(f).replace("\\", "/")
            if normalized not in protected and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed
