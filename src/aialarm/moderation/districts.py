"""Изолированный контур районных новостей: поиск района, модерация и публикация."""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from aialarm.config import district_channel, district_for_id, get_settings
from aialarm.control import get_district_publish_profile
from aialarm.db import session_scope
from aialarm.db.models import DistrictPost, FilteredNews, RawNews
from aialarm.logging import get_logger
from aialarm.media import raw_image_refs
from aialarm.publishers.base import Post
from aialarm.publishers.max import MaxPublisher
from aialarm.rewrite.rewriter import _SCHEMA, _attribution, _build_system
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


def route_district_previews(limit: int = 80, collected_since: datetime | None = None) -> dict[str, int]:
    """Создать районные карточки независимо от статусов главного контура."""
    cfg = get_settings().project.districts
    if not cfg.enabled or not cfg.moderation_max_chat_id:
        return {"to_preview": 0, "unmatched": 0}
    profile = get_district_publish_profile()
    created: list[int] = []
    unmatched = 0
    with session_scope() as session:
        stmt = select(RawNews).join(FilteredNews).where(FilteredNews.relevant.is_(True))
        if collected_since is not None:
            stmt = stmt.where(RawNews.collected_at >= collected_since)
        rows = session.scalars(stmt.order_by(RawNews.collected_at.desc()).limit(limit)).all()
        for raw in rows:
            district_id = detect_district(raw)
            if not district_id or not district_channel(district_id, profile):
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
                publish_profile=profile,
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
        attribution = _attribution(post.raw.source_url, bool(raw_image_refs(post.raw)))
        post.post_text = f"{text}\n\n{attribution}" if attribution else text
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


def publish_district_post(post_id: int) -> tuple[bool, str]:
    """Опубликовать строго в один районный MAX-канал с независимой квотой."""
    with session_scope() as session:
        post = session.get(DistrictPost, post_id)
        if not post or not post.raw or post.status != "moderation":
            return False, "карточка уже обработана"
        profile = post.publish_profile or get_district_publish_profile()
        channel = district_channel(post.district_id, profile)
        if not channel:
            return False, "для района не настроен канал выбранного контура"
        now = datetime.now(timezone.utc)
        cfg = get_settings().project.districts
        limit = cfg.weekend_max_posts if now.astimezone(ZoneInfo("Europe/Moscow")).weekday() >= 5 else cfg.weekday_max_posts
        start = _day_start_utc(now)
        count = session.scalar(
            select(func.count(DistrictPost.id)).where(
                DistrictPost.district_id == post.district_id,
                DistrictPost.status == "published",
                DistrictPost.published_at >= start,
            )
        ) or 0
        if count >= limit:
            return False, f"достигнут лимит района: {limit}"
        previous = session.scalar(
            select(func.max(DistrictPost.published_at)).where(
                DistrictPost.district_id == post.district_id,
                DistrictPost.status == "published",
            )
        )
        if previous and now - previous < timedelta(minutes=cfg.min_minutes_between_posts):
            return False, "не выдержан интервал между постами этого района"
        post.publish_profile = profile
        # Районный пост не должен удерживать десяток оригиналов в RAM: одного фото
        # достаточно для карточки и публикации.
        material = Post(text=post.post_text, image_urls=raw_image_refs(post.raw)[:1])
        result = asyncio.run(MaxPublisher(profile=profile, chat_id=channel).publish(material))
        if not result.ok:
            post.error = result.error
            return False, result.error or "MAX не принял публикацию"
        post.status = "published"
        post.external_id = result.external_id
        post.published_at = now
        post.error = None
        log.info("district_published", post_id=post_id, district=post.district_id, profile=profile)
        return True, ""
