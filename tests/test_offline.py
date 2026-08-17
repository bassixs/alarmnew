"""Офлайн-тесты: работают без сети, ключей и внешних сервисов."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from aialarm.collectors.base import make_dedup_key
from aialarm.db.models import Base, NewsStatus, Publication, PublishStatus, RawNews, RewrittenPost
from aialarm.collectors.dedup import dedup_text
from aialarm.filtering.rules import check_rules
from aialarm.llm.embeddings import HashingTfidfEmbedder, cosine
from aialarm.publishers.base import Post, PublishResult
from aialarm.source_policy import source_matches


def test_dedup_key_stable_and_case_insensitive():
    a = make_dedup_key("https://X.ru/News/1", "Заголовок")
    b = make_dedup_key("https://x.ru/news/1", "заголовок")
    assert a == b


def test_embedder_similarity():
    e = HashingTfidfEmbedder()
    v1 = e.embed("В городе открыли новый парк для жителей района")
    v2 = e.embed("В городе открыли новый парк для жителей района.")
    v3 = e.embed("Курс валют на бирже вырос на два процента")
    assert cosine(v1, v2) > 0.8          # почти идентичные -> дубль
    assert cosine(v1, v3) < cosine(v1, v2)  # разные темы -> ниже


def test_rules_exclude_and_sensitive():
    hit = check_rules(
        "Крупное ДТП на трассе, есть погибшие",
        "подробности",
        exclude=["погиб"],
        sensitive=["ДТП"],
    )
    assert hit.excluded is True
    assert hit.sensitive is True
    assert hit.matched


def test_rules_clean():
    hit = check_rules("Открытие детского сада", "тело", exclude=["погиб"], sensitive=["ДТП"])
    assert not hit.excluded and not hit.sensitive


def test_dedup_text_first_paragraph():
    txt = dedup_text("Заголовок", "Первый абзац.\n\nВторой абзац.")
    assert "Первый абзац" in txt and "Второй" not in txt


def test_post_render_omits_hashtags_and_respects_limit():
    p = Post(text="Текст", hashtags=["город", "#новости"])
    rendered = p.rendered_text(1000)
    assert rendered == "Текст"
    assert len(Post(text="a" * 100).rendered_text(10)) == 10


def test_telegram_network_retry_configuration_is_bounded():
    from aialarm.publishers.telegram import _NETWORK_RETRY_DELAYS_SEC, _NETWORK_SEND_ATTEMPTS

    assert _NETWORK_SEND_ATTEMPTS == 3
    assert len(_NETWORK_RETRY_DELAYS_SEC) == _NETWORK_SEND_ATTEMPTS - 1


def test_source_matches_telegram_post_url():
    assert source_matches("Evgeniy_Serkin", "https://t.me/Evgeniy_Serkin/123")
    assert source_matches("https://t.me/s/Evgeniy_Serkin", "https://t.me/Evgeniy_Serkin/456")
    assert not source_matches("Evgeniy_Serkin", "https://t.me/another_channel/123")


def test_telegram_source_label_has_no_username_prefix():
    from aialarm.rewrite.rewriter import _domain

    assert _domain("https://t.me/Evgeniy_Serkin/123") == "Evgeniy Serkin"


def test_district_footer_is_telegram_only_and_has_clickable_links():
    from aialarm.config import FooterItem
    from aialarm.publishers.footer import render_footer_rows

    rows = [[
        FooterItem(text="Подписаться", url="https://t.me/example"),
        FooterItem(text="📩 Написать нам", url="https://t.me/exampleBot"),
    ]]
    assert render_footer_rows(rows, "markdown") == (
        "[Подписаться](https://t.me/example) [📩 Написать нам](https://t.me/exampleBot)"
    )
    assert render_footer_rows(rows, "html") == (
        '<a href="https://t.me/example">Подписаться</a> '
        '<a href="https://t.me/exampleBot">📩 Написать нам</a>'
    )


def test_source_link_is_clickable_for_each_platform():
    from aialarm.publishers.footer import render_source_link

    url = "https://t.me/example/123"
    assert render_source_link(url, "markdown") == "[Источник](https://t.me/example/123)"
    assert render_source_link(url, "html") == '<a href="https://t.me/example/123">Источник</a>'


def test_district_publish_interval_accepts_sqlite_naive_datetime():
    from aialarm.moderation.districts import _as_utc

    naive = datetime(2026, 8, 6, 11, 0)
    assert _as_utc(naive) == datetime(2026, 8, 6, 11, 0, tzinfo=timezone.utc)


def test_districts_have_no_default_publish_interval():
    from aialarm.config import DistrictsCfg

    assert DistrictsCfg().min_minutes_between_posts == 0


def test_district_publish_profile_is_selected_at_first_delivery():
    from types import SimpleNamespace
    from aialarm.moderation import districts

    original = districts.get_district_publish_profile
    try:
        districts.get_district_publish_profile = lambda: "main"
        assert districts._delivery_profile(SimpleNamespace(publication_results={}, publish_profile="test")) == "main"
        assert districts._delivery_profile(
            SimpleNamespace(publication_results={"max": "123"}, publish_profile="test")
        ) == "test"
    finally:
        districts.get_district_publish_profile = original


def test_district_schedule_cleanup_only_runs_on_scheduled_shift_end():
    from aialarm.pipeline import scheduler

    assert scheduler._is_schedule_transition(True, True, False, False)
    assert not scheduler._is_schedule_transition(True, True, False, True)


def test_district_card_warns_when_source_visual_is_forbidden():
    from aialarm.moderation.notify import _district_prefix

    text = _district_prefix(
        {"district_title": "Таруса", "visual_warning": "у источника запрещён визуал"}
    )
    assert "🚫 ВИЗУАЛ БРАТЬ НЕЛЬЗЯ: у источника запрещён визуал" in text


def test_district_detects_inflected_name_and_keeps_single_best_match():
    from aialarm.config import DistrictCfg, DistrictsCfg, ProjectConfig, Settings, Secrets
    from aialarm.moderation import districts

    settings = Settings(
        secrets=Secrets(),
        project=ProjectConfig(
            districts=DistrictsCfg(
                enabled=True,
                items=[
                    DistrictCfg(id="lyudinovo", title="Людиново", aliases=["Людиновский"]),
                    DistrictCfg(id="borovsk", title="Боровск", aliases=["Боровский"]),
                ],
            )
        ),
    )
    raw = SimpleNamespace(
        title="В Людиновском районе открыли ФАП",
        body="Новый медпункт примет жителей.",
        region="Калужская область",
        source_url="https://example.test/news",
    )
    original_settings = districts.get_settings
    try:
        districts.get_settings = lambda: settings
        assert districts.detect_district(raw) == "lyudinovo"
    finally:
        districts.get_settings = original_settings


def test_district_cards_are_marked_and_offer_max_or_max_plus_telegram():
    from aialarm.moderation import max_client

    preview = max_client.district_preview_buttons(12)
    ready = max_client.district_callback_buttons(12)
    assert preview[0][0]["payload"] == "dpre:rewrite:12"
    payloads = [button["payload"] for row in ready for button in row]
    assert "dmod:approve_max:12" in payloads
    assert "dmod:approve_all:12" in payloads


def test_district_control_buttons_include_independent_schedule_controls():
    from aialarm.moderation import max_client

    payloads = [
        button["payload"] for row in max_client.district_control_buttons() for button in row
    ]
    assert {"dctl:on", "dctl:off", "dctl:auto", "dctl:statistics"}.issubset(payloads)


def test_main_control_panel_offers_manual_city_post_only():
    from aialarm.moderation import max_client

    main_payloads = [button["payload"] for row in max_client.control_buttons() for button in row]
    district_payloads = [
        button["payload"] for row in max_client.district_control_buttons() for button in row
    ]
    assert "ctl:own" in main_payloads
    assert "ctl:own" not in district_payloads


def test_main_ready_card_includes_visual_choice_entrypoint():
    from aialarm.moderation import max_client

    payloads = [button["payload"] for row in max_client.callback_buttons(55) for button in row]
    assert "mod:media:55" in payloads


def test_visual_choices_keep_editor_in_control_and_mark_recommendation():
    from aialarm.moderation import max_client

    buttons = max_client.visual_choice_buttons(
        55,
        {
            "has_original_image": True,
            "generation_available": True,
            "visual_recommendation": "generate",
        },
    )
    by_payload = {button["payload"]: button["text"] for row in buttons for button in row}
    assert set(by_payload).issuperset(
        {"mod:original:55", "mod:generate:55", "mod:none:55", "mod:back:55"}
    )
    assert by_payload["mod:generate:55"].endswith("✓")


def test_visual_style_prompt_requires_ai_label_and_no_other_text():
    from aialarm.visuals import AI_LABEL, _STYLE_PROMPT

    assert AI_LABEL in _STYLE_PROMPT
    assert "Другого текста" in _STYLE_PROMPT
    assert "газетно-комиксная" in _STYLE_PROMPT


def test_generated_or_no_media_never_adds_source_attribution():
    from aialarm.publishers import service

    raw = SimpleNamespace(source_url="https://example.test/news", image_url=None, image_urls=[])
    generated = SimpleNamespace(
        raw=raw,
        media_mode="generated",
        generated_image_path="data/images/generated/test.jpg",
        post_text="Текст",
        source_attribution="— источник: Пример",
        hashtags=[],
    )
    no_media = SimpleNamespace(
        raw=raw,
        media_mode="none",
        generated_image_path=None,
        post_text="Текст",
        source_attribution="— источник: Пример",
        hashtags=[],
    )
    assert service._to_post(generated).text == "Текст"
    assert service._to_post(generated).image_refs() == ["data/images/generated/test.jpg"]
    assert service._to_post(no_media).text == "Текст"
    assert not service._to_post(no_media).image_refs()


def test_manual_card_hides_technical_source_url():
    from aialarm.moderation.notify import _card_text

    card = _card_text(
        {
            "is_sensitive": False,
            "visual_warning": "",
            "confidence": 0,
            "matched_thesis": "",
            "source_url": "",
            "post_text": "Готовый текст",
        }
    )
    assert "источник:" not in card
    assert "Готовый текст" in card


def test_district_quota_buttons_target_only_the_selected_district():
    from aialarm.moderation import max_client

    payloads = [
        button["payload"] for row in max_client.district_quota_buttons("lyudinovo") for button in row
    ]
    assert payloads == ["dquota:stop:lyudinovo", "dquota:continue:lyudinovo"]


def test_district_classifier_uses_local_selection_rules():
    from aialarm.filtering import llm_classifier

    captured: dict = {}

    class FakeClient:
        def structured(self, **kwargs):
            captured.update(kwargs)
            return {"relevant": True, "matched_thesis": "местная новость", "confidence": 70, "reason": "тест"}

    settings = SimpleNamespace(
        project=SimpleNamespace(
            theses=["полезные новости"],
            tone_of_voice="нейтральный",
            llm=SimpleNamespace(classify_model="test-model"),
        )
    )
    old_settings, old_client = llm_classifier.get_settings, llm_classifier.get_llm_client
    try:
        llm_classifier.get_settings = lambda: settings
        llm_classifier.get_llm_client = lambda: FakeClient()
        llm_classifier.classify("Пейзаж Тарусы", "Описание", district_title="Таруса")
        assert "отдельная лента для жителей Таруса" in captured["system"]
        assert "обычный ежедневный прогноз не бери" in captured["system"]
    finally:
        llm_classifier.get_settings, llm_classifier.get_llm_client = old_settings, old_client


def test_plain_language_rewrite_rules_are_district_only():
    from aialarm.rewrite.rewriter import _SYSTEM
    from aialarm.moderation.districts import _DISTRICT_REWRITE_APPENDIX

    assert "что, где и когда" not in _SYSTEM
    assert "обычный житель понял новость с первого прочтения" not in _SYSTEM
    assert "обычный житель понял новость с первого прочтения" in _DISTRICT_REWRITE_APPENDIX


def test_max_edit_returns_to_publish_choice_without_autopublish():
    from aialarm.moderation import max_bot

    calls: list[tuple] = []
    original_apply_edit = max_bot.service.apply_edit
    original_edit_card = max_bot.edit_card
    original_send_card = max_bot.send_card
    original_publish = max_bot.publish_post_id_sync
    try:
        max_bot._edit_state[42] = (7, "mid-7")
        max_bot.service.apply_edit = lambda post_id, text: calls.append(("save", post_id, text)) or True
        max_bot.edit_card = lambda post_id, mid: calls.append(("card", post_id, mid)) or True
        max_bot.send_card = lambda post_id: calls.append(("send", post_id))
        max_bot.publish_post_id_sync = lambda *args: (_ for _ in ()).throw(
            AssertionError("Правка не должна публиковаться автоматически")
        )

        max_bot._handle_message({"sender": {"user_id": 42}, "body": {"text": "Исправленный текст"}})
        assert calls == [("save", 7, "Исправленный текст"), ("card", 7, "mid-7")]
    finally:
        max_bot._edit_state.pop(42, None)
        max_bot.service.apply_edit = original_apply_edit
        max_bot.edit_card = original_edit_card
        max_bot.send_card = original_send_card
        max_bot.publish_post_id_sync = original_publish


def test_max_moderation_message_combines_image_and_buttons():
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from aialarm.moderation import max_client

    sent: dict = {}

    class FakeResponse:
        content = b"{}"

        def __init__(self, data: dict):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, *args, **kwargs):
            raise AssertionError("Локальное изображение не должно скачиваться по сети")

        def post(self, url: str, **kwargs):
            if url.endswith("/uploads"):
                return FakeResponse({"url": "https://upload.test/image"})
            if url == "https://upload.test/image":
                return FakeResponse({"token": "photo-token"})
            sent["body"] = kwargs["json"]
            return FakeResponse({"message": {}})

    original_conn = max_client._conn
    original_client = max_client.httpx.Client
    try:
        max_client._conn = lambda: ("https://botapi.test", "token", "Authorization")
        max_client.httpx.Client = FakeClient
        with TemporaryDirectory() as tmp:
            images = []
            for index in range(9):
                image = Path(tmp) / f"image-{index}.jpg"
                image.write_bytes(f"image-{index}".encode())
                images.append(str(image))
            max_client.send_message(
                "chat",
                "text",
                buttons=[[{"type": "callback", "text": "OK", "payload": "ok"}]],
                image_refs=images,
            )
    finally:
        max_client._conn = original_conn
        max_client.httpx.Client = original_client

    attachment_types = [item["type"] for item in sent["body"]["attachments"]]
    assert attachment_types == ["image"] * 9 + ["inline_keyboard"]


def test_tg_web_extracts_up_to_ten_album_images():
    from bs4 import BeautifulSoup

    from aialarm.collectors.tg_web import TgWebCollector

    photos = "".join(
        f'<a class="tgme_widget_message_photo_wrap" '
        f'style="background-image:url(https://cdn.test/{index}.jpg)"></a>'
        for index in range(12)
    )
    message = BeautifulSoup(f'<div class="tgme_widget_message">{photos}</div>', "html.parser")
    urls = TgWebCollector._extract_images(message)
    assert len(urls) == 10
    assert urls[0].endswith("/0.jpg") and urls[-1].endswith("/9.jpg")


def test_post_image_refs_are_unique():
    post = Post(text="text", image_url="one.jpg", image_urls=["one.jpg", "two.jpg"])
    assert post.image_refs() == ["one.jpg", "two.jpg"]


def test_partial_publication_retries_only_failed_platform():
    """Успешный Telegram не должен получить дубль при повторе MAX."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from aialarm.publishers import service

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    calls: list[str] = []
    results = {"telegram": [True], "max": [False, True]}

    class FakePublisher:
        def __init__(self, platform: str):
            self.platform = platform

        async def publish(self, post: Post) -> PublishResult:
            calls.append(self.platform)
            return PublishResult(ok=results[self.platform].pop(0))

    original_settings = service.get_settings
    original_publisher = service.get_publisher
    original_profile = service.get_publish_profile
    service.get_settings = lambda: SimpleNamespace(
        project=SimpleNamespace(publish=SimpleNamespace(targets=["telegram", "max"]))
    )
    service.get_publish_profile = lambda: "test"
    service.get_publisher = lambda platform, profile=None: FakePublisher(platform)
    try:
        with Session(engine) as session:
            raw = RawNews(
                dedup_key="partial-publish",
                source_type="test",
                source_url="https://example.test/news",
                title="Заголовок",
                body="Текст",
                status=NewsStatus.APPROVED,
            )
            session.add(raw)
            session.flush()
            rewritten = RewrittenPost(raw_id=raw.id, post_text="Пост", hashtags=[])
            session.add(rewritten)
            session.flush()

            assert not asyncio.run(service.publish_post(session, rewritten))
            assert raw.status == NewsStatus.APPROVED
            assert calls == ["telegram", "max"]

            assert asyncio.run(service.publish_post(session, rewritten))
            assert raw.status == NewsStatus.PUBLISHED
            assert calls == ["telegram", "max", "max"]
            assert session.query(Publication).filter_by(
                post_id=rewritten.id, platform="telegram", status=PublishStatus.SUCCESS
            ).count() == 1
    finally:
        service.get_settings = original_settings
        service.get_publisher = original_publisher
        service.get_publish_profile = original_profile


def test_max_only_publication_does_not_retry_telegram():
    """Выбор MAX не должен позже превратиться в автопубликацию в Telegram."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from aialarm.publishers import service

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    calls: list[str] = []

    class FakePublisher:
        def __init__(self, platform: str):
            self.platform = platform

        async def publish(self, post: Post) -> PublishResult:
            calls.append(self.platform)
            return PublishResult(ok=True)

    original_settings = service.get_settings
    original_publisher = service.get_publisher
    original_profile = service.get_publish_profile
    service.get_settings = lambda: SimpleNamespace(
        project=SimpleNamespace(publish=SimpleNamespace(targets=["telegram", "max"]))
    )
    service.get_publish_profile = lambda: "test"
    service.get_publisher = lambda platform, profile=None: FakePublisher(platform)
    try:
        with Session(engine) as session:
            raw = RawNews(
                dedup_key="max-only-publish",
                source_type="test",
                source_url="https://example.test/news",
                title="Заголовок",
                body="Текст",
                status=NewsStatus.APPROVED,
            )
            session.add(raw)
            session.flush()
            rewritten = RewrittenPost(raw_id=raw.id, post_text="Пост", hashtags=[])
            session.add(rewritten)
            session.flush()

            assert asyncio.run(service.publish_post(session, rewritten, ["max"]))
            assert raw.status == NewsStatus.PUBLISHED
            assert calls == ["max"]
            assert session.query(Publication).filter_by(
                post_id=rewritten.id, platform="telegram", status=PublishStatus.SKIPPED
            ).count() == 1
            assert not service._pending_targets(session, rewritten.id)
    finally:
        service.get_settings = original_settings
        service.get_publisher = original_publisher
        service.get_publish_profile = original_profile
