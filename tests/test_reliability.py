"""Regression tests with isolated storage and fake LLM/messenger transports."""

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from aialarm.config import ProjectConfig
from aialarm.db.models import (
    Base,
    DistrictPost,
    ModerationDelivery,
    ModerationFeedback,
    NewsStatus,
    Publication,
    PublishStatus,
    RawNews,
    RewrittenPost,
    utcnow,
)
from aialarm.moderation import delivery, districts, notify, service
from aialarm.publishers import service as publishing
from aialarm.publishers.base import PublishResult
from aialarm.rewrite import rewriter


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)

    @contextmanager
    def scope():
        with Session(engine, expire_on_commit=False) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    cfg = ProjectConfig()
    cfg.publish.targets = ["telegram", "max"]
    cfg.publish.min_minutes_between_posts = 0
    cfg.moderation.admin_chat_id = 123
    cfg.moderation.max_chat_id = "123"
    settings = SimpleNamespace(project=cfg, secrets=SimpleNamespace(telegram_bot_token="fake"))
    for module in (service, delivery, districts, publishing):
        monkeypatch.setattr(module, "session_scope", scope)
    for module in (service, districts, publishing, notify, rewriter):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(publishing, "get_publish_profile", lambda: "test")
    monkeypatch.setattr(service, "_recommend_visual", lambda *args: None)
    monkeypatch.setattr(delivery.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(notify, "send_preview", lambda object_id: None)
    monkeypatch.setattr(notify, "send_district_preview", lambda object_id: None)
    monkeypatch.chdir(tmp_path)
    yield SimpleNamespace(scope=scope, cfg=cfg, settings=settings, engine=engine)
    engine.dispose()


def add_post(env, key="news", status=NewsStatus.MODERATION):
    with env.scope() as session:
        raw = RawNews(
            dedup_key=key,
            source_type="test",
            source_url="https://example.test/1",
            title="Новость",
            body="Оригинал",
            status=status,
        )
        session.add(raw)
        session.flush()
        post = RewrittenPost(raw_id=raw.id, post_text="Версия ИИ", media_mode="none", model="test")
        session.add(post)
        session.flush()
        return raw.id, post.id


def test_preview_failure_retries_after_restart_and_new_shift(isolated, monkeypatch):
    raw_id, _ = add_post(isolated, status=NewsStatus.RELEVANT)
    attempts = []

    def send(object_id):
        attempts.append(object_id)
        if len(attempts) == 1:
            raise RuntimeError("network down")

    monkeypatch.setattr(notify, "send_preview", send)
    service.route_previews()
    with isolated.scope() as session:
        job = session.scalar(select(ModerationDelivery))
        assert job.status == "pending" and job.attempts == 1
        assert session.get(RawNews, raw_id).status == NewsStatus.PREVIEW
        job.next_attempt_at = utcnow() - timedelta(seconds=1)
    # No new candidates in this shift, but the durable retry must still run.
    service.route_previews(collected_since=utcnow() + timedelta(days=1))
    service.route_previews()
    assert attempts == [raw_id, raw_id]
    with isolated.scope() as session:
        assert session.scalar(select(ModerationDelivery)).status == "sent"


def test_delivery_recovers_expired_lease_but_not_live_lease(isolated, monkeypatch):
    raw_id, _ = add_post(isolated, status=NewsStatus.PREVIEW)
    calls = []
    monkeypatch.setattr(notify, "send_preview", calls.append)
    with isolated.scope() as session:
        session.add(
            ModerationDelivery(
                kind="main",
                object_id=raw_id,
                status="sending",
                next_attempt_at=utcnow() + timedelta(minutes=5),
            )
        )
    assert delivery.deliver_previews("main") == 0
    with isolated.scope() as session:
        session.scalar(select(ModerationDelivery)).next_attempt_at = utcnow() - timedelta(seconds=1)
    assert delivery.deliver_previews("main") == 1
    assert calls == [raw_id]


def test_delivery_does_not_resend_rejected_card(isolated, monkeypatch):
    raw_id, _ = add_post(isolated, status=NewsStatus.REJECTED)
    with isolated.scope() as session:
        delivery.enqueue_preview(session, "main", raw_id)
    monkeypatch.setattr(notify, "send_preview", lambda _: pytest.fail("obsolete card sent"))
    assert delivery.deliver_previews("main") == 0
    with isolated.scope() as session:
        assert session.scalar(select(ModerationDelivery)).status == "cancelled"


def test_district_preview_retries_failed_delivery(isolated, monkeypatch):
    raw_id, _ = add_post(isolated)
    with isolated.scope() as session:
        post = DistrictPost(raw_id=raw_id, district_id="district")
        session.add(post)
        session.flush()
        delivery.enqueue_preview(session, "district", post.id)
    monkeypatch.setattr(
        notify, "send_district_preview", lambda _: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    assert delivery.deliver_previews("district") == 0
    with isolated.scope() as session:
        job = session.scalar(select(ModerationDelivery))
        assert job.status == "pending"
        job.next_attempt_at = utcnow() - timedelta(seconds=1)
    monkeypatch.setattr(notify, "send_district_preview", lambda _: None)
    assert delivery.deliver_previews("district") == 1


@pytest.mark.parametrize("limit", ["daily", "interval"])
def test_manual_publication_obeys_limits_and_keeps_platform_choice(isolated, monkeypatch, limit):
    _, previous = add_post(isolated, "previous", NewsStatus.PUBLISHED)
    raw_id, post_id = add_post(isolated, "next")
    with isolated.scope() as session:
        session.add(
            Publication(
                post_id=previous,
                platform="telegram",
                status=PublishStatus.SUCCESS,
                published_at=utcnow(),
            )
        )
        session.add(Publication(post_id=previous, platform="max", status=PublishStatus.SKIPPED))
    if limit == "daily":
        isolated.cfg.publish.max_posts_per_day = 1
    else:
        isolated.cfg.publish.min_minutes_between_posts = 20
    monkeypatch.setattr(publishing, "get_publisher", lambda *args: pytest.fail("limit bypassed"))
    assert not publishing.publish_post_id_sync(
        post_id, ["max"], approve=True, editor_id=42, moderation_platform="max"
    )
    with isolated.scope() as session:
        assert session.get(RawNews, raw_id).status == NewsStatus.APPROVED
        assert session.get(RewrittenPost, post_id).publish_profile == "test"
        assert publishing._pending_targets(session, post_id) == ["max"]
    isolated.cfg.publish.max_posts_per_day = 5
    isolated.cfg.publish.min_minutes_between_posts = 0
    calls = []

    class Publisher:
        async def publish(self, post):
            calls.append("max")
            return PublishResult(ok=True)

    monkeypatch.setattr(publishing, "get_publisher", lambda *args: Publisher())
    assert publishing.run_publish_stage()["published"] == 1
    assert calls == ["max"]


def test_partial_publication_survives_exception_and_daily_quota(isolated, monkeypatch):
    _, post_id = add_post(isolated, status=NewsStatus.APPROVED)
    isolated.cfg.publish.max_posts_per_day = 1
    calls = []

    class Publisher:
        def __init__(self, platform):
            self.platform = platform

        async def publish(self, post):
            calls.append(self.platform)
            if self.platform == "max" and calls.count("max") == 1:
                raise RuntimeError("transport failed")
            return PublishResult(ok=True)

    monkeypatch.setattr(publishing, "get_publisher", lambda platform, profile: Publisher(platform))
    assert not publishing.publish_post_id_sync(post_id)
    assert publishing.publish_post_id_sync(post_id)
    assert calls == ["telegram", "max", "max"]


def test_rewrite_and_edits_preserve_all_text_versions(isolated, monkeypatch):
    raw_id, post_id = add_post(isolated)
    assert service.apply_edit(post_id, "Правка 1", editor_id=11, platform="max")
    assert service.apply_edit(post_id, "Правка 2", editor_id=22, platform="telegram")
    with isolated.scope() as session:
        events = list(session.scalars(select(ModerationFeedback).order_by(ModerationFeedback.id)))
        assert [(e.text_before, e.text_after, e.editor_id) for e in events] == [
            ("Версия ИИ", "Правка 1", "11"),
            ("Правка 1", "Правка 2", "22"),
        ]
        assert all(e.raw_id == raw_id and e.model == "test" for e in events)
        assert session.get(RewrittenPost, post_id).post_text == "Правка 2"


def test_new_ai_rewrite_is_recorded_before_editor_changes(isolated, monkeypatch):
    monkeypatch.setattr(
        rewriter,
        "get_llm_client",
        lambda: SimpleNamespace(structured=lambda **kwargs: {"post_text": "Первый вариант ИИ"}),
    )
    monkeypatch.setattr(rewriter, "_attribution", lambda *args, **kwargs: "")
    with isolated.scope() as session:
        raw = RawNews(
            dedup_key="fresh",
            title="Новость",
            body="Исходник",
            source_type="test",
            source_url="https://example.test",
        )
        session.add(raw)
        session.flush()
        post = rewriter.rewrite_one(session, raw)
        session.flush()
        event = session.scalar(select(ModerationFeedback))
        assert event.post_id == post.id
        assert event.action == "generated" and event.text_after == "Первый вариант ИИ"


@pytest.mark.parametrize("stage", ["selection", "rewrite"])
def test_rejection_records_reason_and_editor(isolated, stage):
    raw_id, post_id = add_post(
        isolated, status=NewsStatus.PREVIEW if stage == "selection" else NewsStatus.MODERATION
    )
    operation = service.cancel_preview if stage == "selection" else service.reject
    object_id = raw_id if stage == "selection" else post_id
    assert operation(object_id, reason="Дубль", editor_id=42, platform="max")
    assert not operation(object_id, reason="Дубль", editor_id=42, platform="max")
    with isolated.scope() as session:
        events = list(session.scalars(select(ModerationFeedback)))
        assert len(events) == 1
        assert (events[0].reason, events[0].editor_id, events[0].stage) == ("Дубль", "42", stage)


def test_district_edit_and_rejection_are_audited(isolated):
    raw_id, _ = add_post(isolated)
    with isolated.scope() as session:
        post = DistrictPost(
            raw_id=raw_id, district_id="district", post_text="ИИ", status="moderation"
        )
        session.add(post)
        session.flush()
        post_id = post.id
    assert districts.edit_district_post(post_id, "Редактор", editor_id=42)
    assert districts.cancel_district_post(post_id, reason="Ошибка в фактах", editor_id=42)
    with isolated.scope() as session:
        events = list(session.scalars(select(ModerationFeedback).order_by(ModerationFeedback.id)))
        assert events[0].text_before == "ИИ" and events[0].text_after == "Редактор"
        assert events[1].reason == "Ошибка в фактах"
        assert all(e.district_post_id == post_id for e in events)


@pytest.mark.parametrize("status", [NewsStatus.PUBLISHED, NewsStatus.REJECTED, NewsStatus.APPROVED])
def test_completed_or_queued_posts_cannot_be_edited_or_rejected(isolated, status):
    _, post_id = add_post(isolated, status=status)
    assert not service.apply_edit(post_id, "Overwrite")
    assert not service.reject(post_id)
    assert not service.select_media(post_id, "none")


def test_telegram_visual_keyboard_requires_choice():
    post = {"media_mode": "unselected", "has_original_image": True, "generation_available": True}
    payloads = [b.callback_data for row in notify._keyboard(3, post).inline_keyboard for b in row]
    assert all(f"mod:{action}:3" in payloads for action in ("original", "generate", "none"))
    assert "mod:approve:3" not in payloads
    post["media_mode"] = "none"
    assert "mod:approve:3" in [
        b.callback_data for row in notify._keyboard(3, post).inline_keyboard for b in row
    ]


@pytest.mark.asyncio
async def test_telegram_callback_selects_visual_and_then_publishes(isolated, monkeypatch):
    from aialarm.moderation import bot

    _, post_id = add_post(isolated)
    with isolated.scope() as session:
        session.get(RewrittenPost, post_id).media_mode = "unselected"
    monkeypatch.setattr(bot, "get_settings", lambda: isolated.settings)
    monkeypatch.setattr(bot, "get_pipeline_state", lambda: SimpleNamespace(active=True))
    monkeypatch.setattr(bot, "_refresh_card", AsyncMock())
    published = []
    monkeypatch.setattr(
        bot, "publish_post_id_sync", lambda *args, **kwargs: published.append(args[0]) or True
    )
    dp = bot.build_dispatcher()
    handler = next(
        h.callback for h in dp.callback_query.handlers if h.callback.__name__ == "on_action"
    )
    cq = SimpleNamespace(
        data=f"mod:none:{post_id}",
        from_user=SimpleNamespace(id=42),
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock(), edit_reply_markup=AsyncMock()),
    )
    await handler(cq, None)
    assert service.media_is_selected(post_id)
    cq.data = f"mod:approve:{post_id}"
    await handler(cq, None)
    assert published == [post_id]


def test_missing_preview_destination_is_retryable(isolated, monkeypatch):
    isolated.cfg.moderation.max_chat_id = ""
    # Call the real low-level sender; no API request can be made without a chat.
    with pytest.raises(RuntimeError, match="max_chat_id"):
        notify._send_preview_max(1)


def test_max_rejection_menu_requires_reason_before_changing_status(isolated, monkeypatch):
    from aialarm.moderation import max_bot

    raw_id, post_id = add_post(isolated)
    monkeypatch.setattr(max_bot.max_client, "answer_callback", lambda *args: None)
    menus = []
    monkeypatch.setattr(max_bot, "_show_rejection", lambda *args, **kwargs: menus.append(args))
    monkeypatch.setattr(max_bot, "finalize_card", lambda *args: True)
    monkeypatch.setattr(
        max_bot, "_submit_post_action", lambda _, operation, *args: operation() or True
    )
    update = {
        "callback": {
            "payload": f"mod:reject:{post_id}",
            "callback_id": "cb",
            "user": {"user_id": 42},
        },
        "message": {"body": {"mid": "mid"}},
    }
    max_bot._handle_callback(update)
    assert menus == [("reason", "reject", post_id, "mid")]
    with isolated.scope() as session:
        assert session.get(RawNews, raw_id).status == NewsStatus.MODERATION
    update["callback"]["payload"] = f"reason:reject:{post_id}:facts"
    max_bot._handle_callback(update)
    with isolated.scope() as session:
        event = session.scalar(select(ModerationFeedback))
        assert event.reason == "Ошибка в фактах" and event.editor_id == "42"
        assert session.get(RawNews, raw_id).status == NewsStatus.REJECTED


def test_publication_lock_serializes_threads_and_releases_on_failure(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from aialarm.publishers.locking import serialized_publication

    monkeypatch.chdir(tmp_path)
    entered, release, second_entered = Event(), Event(), Event()

    @serialized_publication
    def first():
        entered.set()
        assert release.wait(3)
        raise RuntimeError("publisher crashed")

    @serialized_publication
    def second():
        second_entered.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first)
        assert entered.wait(3)
        b = pool.submit(second)
        try:
            assert not second_entered.wait(0.1)
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="publisher crashed"):
            a.result(timeout=3)
        b.result(timeout=3)
    assert second_entered.is_set()


def test_initdb_adds_new_tables_without_changing_existing_posts(isolated, monkeypatch):
    from sqlalchemy import inspect

    from aialarm.db import session as db_session

    raw_id, post_id = add_post(isolated)
    ModerationDelivery.__table__.drop(isolated.engine)
    ModerationFeedback.__table__.drop(isolated.engine)
    monkeypatch.setattr(db_session, "_engine", isolated.engine)
    monkeypatch.setattr(db_session, "_get_factory", lambda: None)
    db_session.init_db()
    db_session.init_db()
    assert {"moderation_deliveries", "moderation_feedback"} <= set(
        inspect(isolated.engine).get_table_names()
    )
    with isolated.scope() as session:
        assert session.get(RawNews, raw_id).status == NewsStatus.MODERATION
        assert session.get(RewrittenPost, post_id).post_text == "Версия ИИ"
