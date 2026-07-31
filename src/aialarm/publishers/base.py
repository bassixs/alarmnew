"""Единый интерфейс публикации и фабрика адаптеров по площадке.

Конвейер сборки поста не зависит от площадки: он строит Post и вызывает
Publisher.publish(post). Форматирование под площадку — забота адаптера.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(slots=True)
class Post:
    text: str
    image_url: str | None = None
    image_urls: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)

    def image_refs(self) -> list[str]:
        refs = list(self.image_urls or []) + ([self.image_url] if self.image_url else [])
        return list(dict.fromkeys(refs))

    def rendered_text(self, max_len: int) -> str:
        # Хэштеги могут пригодиться для внутренней аналитики, но редакционный канал
        # их не публикует: читателю уходит только согласованный текст поста.
        return self.text[:max_len]


@dataclass(slots=True)
class PublishResult:
    ok: bool
    external_id: str | None = None
    error: str | None = None
    rate_limited: bool = False


class Publisher(Protocol):
    platform: str
    async def publish(self, post: Post) -> PublishResult: ...


def get_publisher(platform: str, profile: str | None = None) -> Publisher:
    if platform == "telegram":
        from aialarm.publishers.telegram import TelegramPublisher

        return TelegramPublisher(profile)
    if platform == "max":
        from aialarm.publishers.max import MaxPublisher

        return MaxPublisher(profile)
    raise ValueError(f"Неизвестная площадка публикации: {platform}")
