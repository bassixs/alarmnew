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
    hashtags: list[str] = field(default_factory=list)

    def rendered_text(self, max_len: int) -> str:
        body = self.text
        if self.hashtags:
            tags = " ".join(h if h.startswith("#") else f"#{h}" for h in self.hashtags)
            body = f"{body}\n\n{tags}"
        return body[:max_len]


@dataclass(slots=True)
class PublishResult:
    ok: bool
    external_id: str | None = None
    error: str | None = None
    rate_limited: bool = False


class Publisher(Protocol):
    platform: str
    async def publish(self, post: Post) -> PublishResult: ...


def get_publisher(platform: str) -> Publisher:
    if platform == "telegram":
        from aialarm.publishers.telegram import TelegramPublisher

        return TelegramPublisher()
    if platform == "max":
        from aialarm.publishers.max import MaxPublisher

        return MaxPublisher()
    raise ValueError(f"Неизвестная площадка публикации: {platform}")
