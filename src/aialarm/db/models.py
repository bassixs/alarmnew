"""ORM-модели. Конвейер отражён в статусах RawNews и отдельных таблицах этапов.

Схема совместима и со SQLite (пилот), и с Postgres (прод). Эмбеддинги хранятся
как JSON-массив float — для pgvector в проде можно завести отдельную колонку Vector
(см. dedup.py), не ломая эту таблицу.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class NewsStatus(str, enum.Enum):
    NEW = "new"                    # только собрана
    DUPLICATE = "duplicate"        # отброшена дедупом
    FILTERED_OUT = "filtered_out"  # не прошла фильтр релевантности
    EXCLUDED = "excluded"          # попала под exclude_keywords
    RELEVANT = "relevant"          # прошла фильтр, ждёт рерайта
    REWRITTEN = "rewritten"        # есть черновик поста
    MODERATION = "moderation"      # ждёт решения модератора
    APPROVED = "approved"          # одобрено к публикации
    REJECTED = "rejected"          # отклонено модератором
    PUBLISHED = "published"        # опубликовано хотя бы на одну площадку
    ERROR = "error"


class PublishStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"


class RawNews(Base):
    __tablename__ = "raw_news"
    __table_args__ = (UniqueConstraint("dedup_key", name="uq_raw_news_dedup_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String(64), index=True)  # sha256(url|title)
    source_type: Mapped[str] = mapped_column(String(32))
    source_url: Mapped[str] = mapped_column(Text)
    region: Mapped[str] = mapped_column(String(128), default="")
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    status: Mapped[NewsStatus] = mapped_column(
        Enum(NewsStatus, native_enum=False, length=32), default=NewsStatus.NEW, index=True
    )
    # Эмбеддинг заголовок+первый абзац для дедупа/семантического префильтра.
    embedding: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("raw_news.id"), nullable=True)

    filtered: Mapped["FilteredNews"] = relationship(back_populates="raw", uselist=False)
    rewritten: Mapped["RewrittenPost"] = relationship(back_populates="raw", uselist=False)


class FilteredNews(Base):
    __tablename__ = "filtered_news"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_id: Mapped[int] = mapped_column(ForeignKey("raw_news.id"), unique=True, index=True)
    relevant: Mapped[bool] = mapped_column(Boolean, default=False)
    matched_thesis: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    prefilter_score: Mapped[float] = mapped_column(Float, default=0.0)
    decided_by: Mapped[str] = mapped_column(String(32), default="llm")  # llm | prefilter | rule
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    raw: Mapped[RawNews] = relationship(back_populates="filtered")


class RewrittenPost(Base):
    __tablename__ = "rewritten_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_id: Mapped[int] = mapped_column(ForeignKey("raw_news.id"), unique=True, index=True)
    post_text: Mapped[str] = mapped_column(Text)
    suggested_image_prompt: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[list[str]] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(64), default="")
    edited_by_moderator: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    raw: Mapped[RawNews] = relationship(back_populates="rewritten")
    publications: Mapped[list["Publication"]] = relationship(back_populates="post")


class Publication(Base):
    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("rewritten_posts.id"), index=True)
    platform: Mapped[str] = mapped_column(String(16))  # telegram | max
    status: Mapped[PublishStatus] = mapped_column(
        Enum(PublishStatus, native_enum=False, length=16), default=PublishStatus.PENDING
    )
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # message id
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    post: Mapped[RewrittenPost] = relationship(back_populates="publications")
