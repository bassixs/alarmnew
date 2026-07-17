"""Офлайн-тесты: работают без сети, ключей и внешних сервисов."""
from __future__ import annotations

from aialarm.collectors.base import make_dedup_key
from aialarm.collectors.dedup import dedup_text
from aialarm.filtering.rules import check_rules
from aialarm.llm.embeddings import HashingTfidfEmbedder, cosine
from aialarm.publishers.base import Post


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
