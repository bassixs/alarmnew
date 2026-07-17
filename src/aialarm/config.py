"""Загрузка конфигурации: секреты из окружения (.env), параметры проекта из YAML.

Разделение намеренное: секреты никогда не попадают в config.yaml (который можно
коммитить как пример), а бизнес-параметры — не в переменные окружения.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── Секреты (окружение / .env) ───────────────────────────────────────────────
class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM-шлюз. По умолчанию — AiTunnel (OpenAI-совместимый прокси).
    # Подойдёт и обычный OpenAI (base_url=https://api.openai.com/v1), и любой совместимый.
    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_base_url: str = Field(default="https://api.aitunnel.ru/v1", alias="LLM_BASE_URL")
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_api_id: int | None = Field(default=None, alias="TELEGRAM_API_ID")
    telegram_api_hash: str = Field(default="", alias="TELEGRAM_API_HASH")
    telethon_session: str = Field(default="aialarm", alias="TELETHON_SESSION")
    max_bot_token: str = Field(default="", alias="MAX_BOT_TOKEN")
    database_url: str = Field(default="sqlite:///./aialarm.db", alias="DATABASE_URL")
    config_path: str = Field(default="./config.yaml", alias="AIALARM_CONFIG")


# ── Параметры проекта (YAML) ─────────────────────────────────────────────────
class SourceCfg(BaseModel):
    type: str  # rss | telegram | scrape | aggregator
    url: str
    region: str = ""
    poll_interval_min: int = 15
    enabled: bool = True
    extra: dict = Field(default_factory=dict)  # напр. {"selectors": {"body": "div.article"}}


class ChannelsCfg(BaseModel):
    telegram: str = ""
    max: str = ""


class FilterCfg(BaseModel):
    strategy: str = "prefilter_then_llm"
    embed_relevance_min: float = 0.35
    llm_confidence_min: int = 70
    dedup_cosine_threshold: float = 0.85


class ModerationCfg(BaseModel):
    enabled: bool = True
    mode: str = "all"  # all | sensitive_only | off
    platform: str = "telegram"   # где модерируем: telegram | max
    admin_chat_id: int = 0       # Telegram-чат модерации
    max_chat_id: str = ""        # MAX-чат модерации (если platform == max)
    auto_publish_after_min: int = 0
    auto_publish_low_risk_on_timeout: bool = False


class FooterItem(BaseModel):
    text: str
    url: str = ""


class PublishCfg(BaseModel):
    targets: list[str] = Field(default_factory=lambda: ["telegram"])
    max_posts_per_day: int = 5
    min_minutes_between_posts: int = 20
    attribution_template: str = "по данным {source}"
    ai_disclosure: str = ""
    # Подвал под каждую площадку: список строк, строка = список ссылок {text,url}.
    # Пустой список внутри = пустая строка-разделитель.
    footers: dict[str, list[list[FooterItem]]] = Field(default_factory=dict)


class LLMCfg(BaseModel):
    classify_model: str = "minimax-m3"
    rewrite_model: str = "claude-sonnet-4.5"
    combined_call: bool = False
    max_tokens: int = 1500
    temperature: float = 0.3  # рерайт — немного «живости»; классификатор форсит 0 в коде


class MaxPlatformCfg(BaseModel):
    base_url: str = "https://botapi.max.ru"
    auth_header: str = "Authorization"


class ProjectConfig(BaseModel):
    project_name: str = "aialarm"
    channels: ChannelsCfg = Field(default_factory=ChannelsCfg)
    sources: list[SourceCfg] = Field(default_factory=list)
    theses: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    sensitive_keywords: list[str] = Field(default_factory=list)
    tone_of_voice: str = ""
    tone_examples: list[str] = Field(default_factory=list)
    post_length: str = "600-900 знаков"
    posting_frequency: str = "3-5 постов в день"
    filter: FilterCfg = Field(default_factory=FilterCfg)
    moderation: ModerationCfg = Field(default_factory=ModerationCfg)
    publish: PublishCfg = Field(default_factory=PublishCfg)
    llm: LLMCfg = Field(default_factory=LLMCfg)
    max_platform: MaxPlatformCfg = Field(default_factory=MaxPlatformCfg)


class Settings(BaseModel):
    secrets: Secrets
    project: ProjectConfig


def _load_yaml(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    secrets = Secrets()
    raw = _load_yaml(secrets.config_path)
    project = ProjectConfig(**raw)
    return Settings(secrets=secrets, project=project)


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
