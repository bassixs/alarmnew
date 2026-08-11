"""Изолированный контур районных новостей: поиск района, модерация и публикация."""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from aialarm.config import (
    district_channel,
    district_for_id,
    district_telegram_footer,
    district_telegram_channel,
    get_settings,
)
from aialarm.control import get_district_publish_profile
from aialarm.db import session_scope
from aialarm.db.models import DistrictDailyControl, DistrictPost, FilteredNews, NewsStatus, RawNews
from aialarm.logging import get_logger
from aialarm.media import raw_image_refs
from aialarm.publishers.base import Post
from aialarm.publishers.max import MaxPublisher
from aialarm.publishers.telegram import TelegramPublisher
from aialarm.rewrite.rewriter import _SCHEMA, _build_system
from aialarm.llm.client import get_llm_client
from aialarm.source_policy import source_matches, visual_policy

log = get_logger(__name__)

_DISTRICT_REWRITE_APPENDIX = """

Дополнительные правила для районного канала:
- Пиши так, чтобы обычный житель понял новость с первого прочтения: простыми словами,
  короткими предложениями и без языка пресс-релизов.
- Заголовок должен ясно называть суть и место, когда оно известно. Не используй кликбейт,
  абстрактные «важные новости» и служебные формулировки.
- Первый абзац сразу отвечает на вопросы «что, где и когда». Для отключений, работ,
  изменений маршрута или расписания сначала называй дату/время и конкретное место, затем
  коротко объясняй причину и что будет дальше.
- После первого абзаца оставляй только важные детали: сроки, адреса и действия, которые
  нужны читателю. Не повторяй сведения разными словами.
- Убирай обращения вроде «уважаемые жители», общие вводные фразы и канцелярит. Не пиши
  «в рамках», «в целях», «данное мероприятие», «осуществляется» — замени понятными словами.
- Эмодзи не обязательны. Если они помогают быстро считать смысл заголовка, используй один
  уместный (например, ℹ️ для объявления), но не украшай ими каждую новость.
- Пиши для жителей именно этого района: называй конкретный населённый пункт, улицу,
  учреждение или событие, если они есть в исходнике.
- Не заменяй локальный контекст общими словами «в регионе» или «в области».
- Для небольших, но полезных местных новостей (школы, культура, спорт, ЖКХ, дороги,
  расписания, благоустройство) выбирай практичную суть: что произошло и кого это касается.
- Не преувеличивай масштаб районной новости и не делай её похожей на официальный пресс-релиз.
"""

_LEGACY_SOURCE_LINE = re.compile(r"^— источник:\\s*.+$", re.IGNORECASE)


def _source_district(raw: RawNews) -> str:
    for source in get_settings().project.sources:
        if source.district_id and source_matches(source.url, raw.source_url):
            return source.district_id
    return ""


def detect_district(raw: RawNews) -> str:
    """Вернуть один наиболее явно упомянутый район; сомнительные новости не шлём."""
    direct = _source_district(raw)
    if direct and district_for_id(direct):
        return direct
    text = " ".join((raw.title, raw.body, raw.region)).casefold()
    best_id, best_score = "", 0
    for district in get_settings().project.districts.items:
        score = 0
        for raw_alias in {district.title, *district.aliases}:
            alias = raw_alias.strip().casefold()
            forms = {alias}
            if alias.endswith(("ий", "ый")):
                forms.add(alias[:-2])
            for form in forms:
                if len(form) >= 4 and form in text:
                    score += 2 if form in raw.title.casefold() else 1
        if score > best_score:
            best_id, best_score = district.id, score
    return best_id


def _local_day(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("Europe/Moscow")).date()


def _daily_control(
    session, district_id: str, now: datetime | None = None
) -> DistrictDailyControl | None:
    return session.scalar(
        select(DistrictDailyControl).where(
            DistrictDailyControl.district_id == district_id,
            DistrictDailyControl.local_date == _local_day(now),
        )
    )


def current_district_post_limit(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    cfg = get_settings().project.districts
    return (
        cfg.weekend_max_posts
        if now.astimezone(ZoneInfo("Europe/Moscow")).weekday() >= 5
        else cfg.weekday_max_posts
    )


def district_daily_summary(
    profile: str | None = None, now: datetime | None = None
) -> list[dict[str, str | int]]:
    """Сводка публикаций и режима поиска по районам за текущий московский день."""
    now = now or datetime.now(timezone.utc)
    profile = profile or get_district_publish_profile()
    items = [
        item
        for item in get_settings().project.districts.items
        if item.enabled and district_channel(item.id, profile)
    ]
    if not items:
        return []
    ids = {item.id for item in items}
    with session_scope() as session:
        counts = dict(
            session.execute(
                select(DistrictPost.district_id, func.count(DistrictPost.id))
                .where(
                    DistrictPost.district_id.in_(ids),
                    DistrictPost.status.in_(("published", "partial")),
                    DistrictPost.published_at >= _day_start_utc(now),
                )
                .group_by(DistrictPost.district_id)
            ).all()
        )
        controls = {
            control.district_id: control.search_mode
            for control in session.scalars(
                select(DistrictDailyControl).where(
                    DistrictDailyControl.district_id.in_(ids),
                    DistrictDailyControl.local_date == _local_day(now),
                )
            )
        }
    limit = current_district_post_limit(now)
    return [
        {
            "id": item.id,
            "title": item.title,
            "published": int(counts.get(item.id, 0)),
            "limit": limit,
            "search_mode": controls.get(item.id, "active"),
        }
        for item in items
    ]


def active_district_ids(profile: str | None = None, now: datetime | None = None) -> set[str]:
    """Районы, для которых сегодня ещё нужно искать и обрабатывать новости."""
    profile = profile or get_district_publish_profile()
    candidates = {
        item.id
        for item in get_settings().project.districts.items
        if item.enabled and district_channel(item.id, profile)
    }
    if not candidates:
        return set()
    with session_scope() as session:
        paused = set(session.scalars(
            select(DistrictDailyControl.district_id).where(
                DistrictDailyControl.local_date == _local_day(now),
                DistrictDailyControl.search_mode == "paused",
                DistrictDailyControl.district_id.in_(candidates),
            )
        ))
    return candidates - paused


def set_district_search_mode(district_id: str, mode: str, now: datetime | None = None) -> bool:
    """Остановить район до конца дня или разрешить работать сверх дневной нормы."""
    if mode not in {"stop", "continue"} or not district_for_id(district_id):
        return False
    now = now or datetime.now(timezone.utc)
    with session_scope() as session:
        control = _daily_control(session, district_id, now)
        if control is None:
            control = DistrictDailyControl(
                district_id=district_id,
                local_date=_local_day(now),
                quota_reached_at=now,
            )
            session.add(control)
        control.search_mode = "continued" if mode == "continue" else "paused"
        control.updated_at = now
    return True


def route_district_previews(
    limit: int = 80,
    collected_since: datetime | None = None,
    allowed_district_ids: set[str] | None = None,
) -> dict[str, int]:
    """Создать районные карточки независимо от статусов главного контура."""
    cfg = get_settings().project.districts
    if not cfg.enabled or not cfg.moderation_max_chat_id:
        return {"to_preview": 0, "unmatched": 0}
    profile = get_district_publish_profile()
    allowed_district_ids = (
        active_district_ids(profile) if allowed_district_ids is None else allowed_district_ids
    )
    created: list[int] = []
    unmatched = 0
    with session_scope() as session:
        stmt = select(RawNews).join(FilteredNews).where(FilteredNews.relevant.is_(True))
        if collected_since is not None:
            stmt = stmt.where(RawNews.collected_at >= collected_since)
        rows = session.scalars(stmt.order_by(RawNews.collected_at.desc()).limit(limit)).all()
        for raw in rows:
            district_id = detect_district(raw)
            if (
                not district_id
                or district_id not in allowed_district_ids
                or not district_channel(district_id, profile)
            ):
                unmatched += 1
                continue
            exists = session.scalar(
                select(DistrictPost.id).where(
                    DistrictPost.raw_id == raw.id, DistrictPost.district_id == district_id
                )
            )
            if exists:
                continue
            candidate = DistrictPost(
                raw_id=raw.id,
                district_id=district_id,
                status="preview",
            )
            session.add(candidate)
            session.flush()
            created.append(candidate.id)

    from aialarm.moderation.notify import send_district_preview
    import time as wait

    for index, post_id in enumerate(created):
        if index:
            wait.sleep(2)  # MAX допускает 2 сообщения/секунду в один чат
        try:
            send_district_preview(post_id)
        except Exception as exc:  # noqa: BLE001
            log.error("district_preview_notify_failed", post_id=post_id, error=str(exc))
    log.info("district_preview_routing_done", to_preview=len(created), unmatched=unmatched)
    return {"to_preview": len(created), "unmatched": unmatched}


def _card_data(post_id: int) -> dict | None:
    with session_scope() as session:
        post = session.get(DistrictPost, post_id)
        if not post or not post.raw:
            return None
        district = district_for_id(post.district_id)
        fn = session.scalar(select(FilteredNews).where(FilteredNews.raw_id == post.raw_id))
        _, visual_warning = visual_policy(post.raw.source_url)
        images = raw_image_refs(post.raw)
        return {
            "post_id": post.id,
            "district_id": post.district_id,
            "district_title": district.title if district else post.district_id,
            "status": post.status,
            "title": post.raw.title,
            "body": post.raw.body,
            "post_text": post.post_text,
            "source_url": post.raw.source_url,
            "confidence": fn.confidence if fn else 0,
            "matched_thesis": fn.matched_thesis if fn else "",
            "is_sensitive": fn.is_sensitive if fn else False,
            "image_urls": images,
            "has_image": bool(images),
            "visual_warning": visual_warning,
        }


def get_district_preview(post_id: int) -> dict | None:
    return _card_data(post_id)


def get_district_pending(post_id: int) -> dict | None:
    return _card_data(post_id)


def rewrite_district_post(post_id: int) -> bool:
    with session_scope() as session:
        post = session.get(DistrictPost, post_id)
        if not post or not post.raw or post.status not in {"preview", "moderation"}:
            return False
        district = district_for_id(post.district_id)
        if not district:
            return False
        llm = get_settings().project.llm
        district_system = _build_system(f"{district.title} — новости района") + _DISTRICT_REWRITE_APPENDIX
        data = get_llm_client().structured(
            model=llm.rewrite_model,
            system=district_system,
            user=f'Новость: "{post.raw.title}. {(post.raw.body or "")[:4000]}"',
            schema=_SCHEMA,
            max_tokens=llm.max_tokens,
            temperature=llm.temperature,
        )
        text = str(data.get("post_text", "")).strip()
        # Источник добавляется при публикации: кликабельной ссылкой и всегда, даже без фото.
        post.post_text = text
        post.model = llm.rewrite_model
        post.status = "moderation"
        log.info("district_rewritten", post_id=post_id, district=post.district_id)
        return True


def cancel_district_post(post_id: int) -> bool:
    with session_scope() as session:
        post = session.get(DistrictPost, post_id)
        if not post or post.status in {"published", "rejected"}:
            return False
        post.status = "rejected"
        if post.raw:
            # После завершения смены изображение отклонённой карточки можно безопасно убрать.
            post.raw.status = NewsStatus.REJECTED
        return True


def edit_district_post(post_id: int, text: str) -> bool:
    with session_scope() as session:
        post = session.get(DistrictPost, post_id)
        if not post or post.status in {"published", "rejected"}:
            return False
        post.post_text = text.strip()
        post.edited_by_moderator = True
        post.status = "moderation"
        return True


def _day_start_utc(now: datetime) -> datetime:
    local = now.astimezone(ZoneInfo("Europe/Moscow"))
    return datetime.combine(local.date(), time.min, tzinfo=local.tzinfo).astimezone(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """SQLite может вернуть timezone=True значение без tzinfo."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _delivery_profile(post: DistrictPost) -> str:
    """Выбрать контур в момент первой доставки.

    Карточка может ждать модератора после переключения тест/основной, поэтому до
    первой успешной отправки используем текущий выбранный контур. При частичном
    успехе фиксируем прежний контур: повтор не должен внезапно уйти в другие каналы.
    """
    if post.publication_results:
        return post.publish_profile or get_district_publish_profile()
    return get_district_publish_profile()


def publish_district_post(
    post_id: int, selected_targets: list[str] | None = None
) -> tuple[bool, str]:
    """Опубликовать районный пост в MAX либо одновременно в MAX и Telegram.

    Успешная площадка сохраняется отдельно в JSON, поэтому повтор после частичного
    сбоя отправит пост только туда, куда он ещё не дошёл.
    """
    targets = list(dict.fromkeys(selected_targets or ["max"]))
    if not targets or any(target not in {"max", "telegram"} for target in targets):
        return False, "неверно выбраны площадки"
    if "max" not in targets:
        return False, "районный пост должен публиковаться в MAX"
    with session_scope() as session:
        post = session.get(DistrictPost, post_id)
        if not post or not post.raw or post.status not in {"moderation", "partial"}:
            return False, "карточка уже обработана"
        profile = _delivery_profile(post)
        max_channel = district_channel(post.district_id, profile)
        telegram_channel = district_telegram_channel(post.district_id, profile)
        if not max_channel:
            return False, "для района не настроен MAX-канал выбранного контура"
        if "telegram" in targets and not telegram_channel:
            return False, "для района не настроен Telegram-канал выбранного контура"
        now = datetime.now(timezone.utc)
        cfg = get_settings().project.districts
        limit = current_district_post_limit(now)
        start = _day_start_utc(now)
        count = session.scalar(
            select(func.count(DistrictPost.id)).where(
                DistrictPost.district_id == post.district_id,
                DistrictPost.status.in_(("published", "partial")),
                DistrictPost.id != post.id,
                DistrictPost.published_at >= start,
            )
        ) or 0
        control = _daily_control(session, post.district_id, now)
        if count >= limit and (not control or control.search_mode != "continued"):
            return False, f"дневная норма {limit} уже выполнена: поиск района остановлен"
        previous = session.scalar(
            select(func.max(DistrictPost.published_at)).where(
                DistrictPost.district_id == post.district_id,
                DistrictPost.status.in_(("published", "partial")),
                DistrictPost.id != post.id,
            )
        )
        if previous and now - _as_utc(previous) < timedelta(minutes=cfg.min_minutes_between_posts):
            return False, "не выдержан интервал между постами этого района"
        post.publish_profile = profile
        # Районный пост не должен удерживать десяток оригиналов в RAM: одного фото
        # достаточно для карточки и публикации.
        # Карточки, переписанные до появления кликабельного источника, могли содержать
        # старую текстовую подпись «— источник: ...». Убираем только её, не трогая текст
        # модератора, и выводим единый кликабельный вариант ниже.
        publish_text = "\n".join(
            line for line in post.post_text.splitlines() if not _LEGACY_SOURCE_LINE.match(line.strip())
        ).strip()
        material = Post(
            text=publish_text,
            source_url=post.raw.source_url,
            image_urls=raw_image_refs(post.raw)[:1],
        )
        delivered = dict(post.publication_results or {})
        errors: list[str] = []
        if "max" in targets and "max" not in delivered:
            result = asyncio.run(
                MaxPublisher(profile=profile, chat_id=max_channel, footer_rows=[]).publish(material)
            )
            if result.ok:
                delivered["max"] = result.external_id or "ok"
            else:
                errors.append(f"MAX: {result.error or 'не принял публикацию'}")
        if "telegram" in targets and "telegram" not in delivered:
            result = asyncio.run(
                TelegramPublisher(
                    profile=profile,
                    chat_id=telegram_channel,
                    footer_rows=district_telegram_footer(post.district_id),
                ).publish(material)
            )
            if result.ok:
                delivered["telegram"] = result.external_id or "ok"
            else:
                errors.append(f"Telegram: {result.error or 'не принял публикацию'}")
        post.publication_results = delivered
        if errors:
            post.status = "partial" if delivered else "moderation"
            post.published_at = now if delivered else None
            post.external_id = delivered.get("max") or delivered.get("telegram")
            post.error = "; ".join(errors)
            return False, post.error
        post.status = "published"
        # Районный RawNews иначе остаётся RELEVANT навсегда и удерживает локальное фото.
        # Финально опубликованная карточка больше не нуждается в кэше изображения.
        post.raw.status = NewsStatus.PUBLISHED
        post.external_id = delivered.get("max") or delivered.get("telegram")
        post.published_at = now
        post.error = None
        target_reached = count + 1 >= limit and control is None
        if target_reached:
            session.add(
                DistrictDailyControl(
                    district_id=post.district_id,
                    local_date=_local_day(now),
                    search_mode="paused",
                    quota_reached_at=now,
                )
            )
        log.info("district_published", post_id=post_id, district=post.district_id, profile=profile)
        return True, "daily_target_reached" if target_reached else ""
