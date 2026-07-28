"""Офлайн-тесты: работают без сети, ключей и внешних сервисов."""
from __future__ import annotations

from aialarm.collectors.base import make_dedup_key
from aialarm.collectors.dedup import dedup_text
from aialarm.filtering.rules import check_rules
from aialarm.llm.embeddings import HashingTfidfEmbedder, cosine
from aialarm.publishers.base import Post
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


def test_post_render_with_hashtags_and_limit():
    p = Post(text="Текст", hashtags=["город", "#новости"])
    rendered = p.rendered_text(1000)
    assert "#город" in rendered and "#новости" in rendered
    assert len(Post(text="a" * 100).rendered_text(10)) == 10


def test_source_matches_telegram_post_url():
    assert source_matches("Evgeniy_Serkin", "https://t.me/Evgeniy_Serkin/123")
    assert source_matches("https://t.me/s/Evgeniy_Serkin", "https://t.me/Evgeniy_Serkin/456")
    assert not source_matches("Evgeniy_Serkin", "https://t.me/another_channel/123")


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
            image = Path(tmp) / "image.jpg"
            image.write_bytes(b"image-bytes")
            max_client.send_message(
                "chat",
                "text",
                buttons=[[{"type": "callback", "text": "OK", "payload": "ok"}]],
                image_ref=str(image),
            )
    finally:
        max_client._conn = original_conn
        max_client.httpx.Client = original_client

    attachment_types = [item["type"] for item in sent["body"]["attachments"]]
    assert attachment_types == ["image", "inline_keyboard"]
