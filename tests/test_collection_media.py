"""A slow image host must not block persistence or other collection passes."""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from aialarm.collectors import images, store
from aialarm.collectors.base import CollectedItem
from aialarm.config import ProjectConfig
from aialarm.db.models import Base, NewsStatus, RawNews


@pytest.fixture
def image_env(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "IMAGES_DIR", tmp_path / "images")
    monkeypatch.setattr(images, "_host_retry_after", {})
    return tmp_path


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(images.httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_stalled_cdn_has_hard_deadline_and_cooldown(image_env, monkeypatch):
    monkeypatch.setattr(images, "_IMAGE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(images, "_MAX_CONCURRENT_DOWNLOADS", 1)
    calls = []

    async def stalled(request):
        calls.append(str(request.url))
        await asyncio.sleep(60)
        return httpx.Response(200, content=b"photo")

    transport(monkeypatch, stalled)
    async with asyncio.timeout(1):
        assert await images.download_images({str(i): f"https://cdn.test/{i}.jpg" for i in range(30)}) == {}
        assert await images.download_images({"retry": "https://cdn.test/retry.jpg"}) == {}
    assert len(calls) == 1
    assert not list(images.IMAGES_DIR.iterdir())


@pytest.mark.asyncio
async def test_whole_image_batch_has_deadline_and_cancels_requests(image_env, monkeypatch):
    monkeypatch.setattr(images, "_BATCH_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(images, "_IMAGE_TIMEOUT_SECONDS", 60)
    active = set()

    async def stalled(request):
        active.add(str(request.url))
        try:
            await asyncio.sleep(60)
        finally:
            active.remove(str(request.url))
        return httpx.Response(200, content=b"photo")

    transport(monkeypatch, stalled)
    async with asyncio.timeout(1):
        assert await images.download_images({str(i): f"https://cdn{i}.test/p.jpg" for i in range(30)}) == {}
    assert not active


@pytest.mark.asyncio
async def test_cached_files_are_reused_and_oversized_images_are_skipped(image_env, monkeypatch):
    monkeypatch.setattr(images, "_MAX_BYTES", 10)
    calls = []

    async def download(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=b"photo" if request.url.path == "/ok" else b"x" * 20)

    transport(monkeypatch, download)
    saved = await images.download_images({"one": "https://cdn.test/ok", "large": "https://cdn.test/large"})
    assert list(saved) == ["one"]
    assert (images.IMAGES_DIR / "one.jpg").read_bytes() == b"photo"
    assert not (images.IMAGES_DIR / "large.jpg").exists()
    assert await images.download_images({"one": "https://cdn.test/ok"}) == saved
    assert calls == ["https://cdn.test/ok", "https://cdn.test/large"]


@pytest.mark.asyncio
async def test_text_commits_before_download_and_no_db_transaction_during_network(image_env, monkeypatch):
    engine = create_engine(f"sqlite:///{image_env / 'news.db'}")
    Base.metadata.create_all(engine)
    sessions = []

    @contextmanager
    def scope():
        with Session(engine, expire_on_commit=False) as session:
            sessions.append(session)
            try:
                yield session
                session.commit()
            finally:
                sessions.remove(session)

    monkeypatch.setattr(store, "session_scope", scope)
    monkeypatch.setattr(store, "get_settings", lambda: SimpleNamespace(project=ProjectConfig()))
    monkeypatch.setattr(store, "get_embedder", lambda: SimpleNamespace(embed=lambda _: [1.0, 0.0]))
    monkeypatch.setattr(store, "find_semantic_duplicate", lambda *args: (None, 0))
    monkeypatch.setattr(store, "visual_allowed", lambda _: True)
    monkeypatch.setattr(store, "raw_image_refs", lambda raw: list(raw.image_urls or []))
    items = [CollectedItem(source_type="tg_web", source_url="https://t.me/test", region="",
                           title=f"News {i}", item_url=f"https://t.me/test/{i}",
                           image_urls=[f"https://cdn.test/{i}.jpg"]) for i in range(2)]
    assert store.store_items(items)["inserted"] == 2

    async def download(request):
        assert sessions == [], "network request inside a DB session"
        with Session(engine) as session:
            assert len(list(session.scalars(select(RawNews)))) == 2
        return httpx.Response(200, content=b"photo")

    transport(monkeypatch, download)
    assert await store.cache_collected_images(items) == 2
    with scope() as session:
        rows = list(session.scalars(select(RawNews)))
        assert all(len(row.image_urls) == 1 for row in rows)
        for row in rows:
            row.status = NewsStatus.PUBLISHED
    for path in images.IMAGES_DIR.iterdir():
        path.unlink()
    # Completed posts must not trigger backfill downloads on every poll.
    assert store.store_items(items)["exact_dup"] == 2
    assert await store.cache_collected_images(items) == 0
    engine.dispose()
