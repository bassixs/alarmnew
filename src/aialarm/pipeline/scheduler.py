"""Планировщик пилота на APScheduler (in-process, без Redis).

Джобы:
- по одному на источник, с его poll_interval_min — только сбор;
- processing каждые PROCESS_INTERVAL_MIN — фильтр/рерайт/маршрутизация модерации;
- publish каждые min_minutes_between_posts — публикация одобренных с учётом лимитов.

Прод: заменить на Celery beat (расписание) + воркеры (стадии), логика та же.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

from aialarm.config import get_settings
from aialarm.control import (
    get_district_pipeline_state,
    get_district_publish_profile,
    get_pipeline_state,
    render_control_status,
    render_district_control_status,
)
from aialarm.db import init_db
from aialarm.collectors.images import cleanup_old
from aialarm.filtering import run_filter_stage
from aialarm.logging import configure_logging, get_logger
from aialarm.moderation.service import route_previews
from aialarm.moderation.districts import active_district_ids, route_district_previews
from aialarm.pipeline.runner import run_collection_sync
from aialarm.publishers.service import run_publish_stage

log = get_logger(__name__)

PROCESS_INTERVAL_MIN = 5
_last_active: bool | None = None
_last_scheduled_active: bool | None = None
_last_district_active: bool | None = None
_last_district_scheduled_active: bool | None = None
_source_last_collected: dict[str, datetime] = {}


def _collection_job() -> None:
    state = get_pipeline_state()
    district_state = get_district_pipeline_state()
    if not state.active and not district_state.active:
        log.info("collection_skipped_outside_duty", mode=state.mode, district_mode=district_state.mode)
        return
    now = datetime.now(timezone.utc)
    sources = get_settings().project.sources
    district_profile = get_district_publish_profile()
    active_districts = active_district_ids(district_profile, now) if district_state.active else set()
    due = {
        source.url
        for source in sources
        if source.enabled
        and (
            (not source.district_id and state.active)
            or (
                source.district_id
                and district_state.active
                and source.district_id in active_districts
            )
        )
        and (
            source.url not in _source_last_collected
            or now - _source_last_collected[source.url]
            >= timedelta(minutes=max(1, source.poll_interval_min))
        )
    }
    if not due:
        return
    main_due = {source.url for source in sources if source.url in due and not source.district_id}
    district_due = {source.url for source in sources if source.url in due and source.district_id}
    if main_due:
        run_collection_sync(source_urls=main_due, published_since=state.active_since)
    if district_due:
        run_collection_sync(source_urls=district_due, published_since=district_state.active_since)
    # Ошибочный источник не должен мгновенно забивать лог повторными запросами.
    for source_url in due:
        _source_last_collected[source_url] = now


def _processing_job() -> None:
    state = get_pipeline_state()
    district_state = get_district_pipeline_state()
    if not state.active and not district_state.active:
        log.info("processing_skipped_outside_duty", mode=state.mode, district_mode=district_state.mode)
        return
    if state.active:
        run_filter_stage(collected_since=state.active_since, district_only=False)
        route_previews(collected_since=state.active_since)
    if district_state.active:
        profile = get_district_publish_profile()
        active_districts = active_district_ids(profile)
        if active_districts:
            run_filter_stage(
                collected_since=district_state.active_since,
                district_only=True,
            )
            route_district_previews(
                collected_since=district_state.active_since,
                allowed_district_ids=active_districts,
            )
    cleanup_old(days=1)       # чистим старые скачанные картинки


def _publish_job() -> None:
    state = get_pipeline_state()
    if not state.active:
        log.info("publish_skipped_outside_duty", mode=state.mode)
        return
    run_publish_stage()


def _notify_control_transition(active: bool) -> None:
    try:
        from aialarm.moderation import max_client

        chat_id = get_settings().project.moderation.max_chat_id
        if chat_id:
            prefix = (
                "🟢 Смена помощника началась\n\n"
                "🔎 Проверяю источники автоматически. Карточки появятся, "
                "когда найдутся подходящие свежие новости.\n\n"
                if active
                else "⚪ Смена помощника завершена\n\n"
            )
            max_client.send_message(
                chat_id,
                prefix + render_control_status(),
                buttons=max_client.control_buttons(),
            )
    except Exception as e:  # noqa: BLE001
        log.warning("control_transition_notify_failed", error=str(e))


def _notify_district_control_transition(active: bool) -> None:
    """Сообщить в отдельный районный чат о границе его собственной смены."""
    try:
        from aialarm.moderation import max_client

        chat_id = get_settings().project.districts.moderation_max_chat_id
        if not chat_id:
            return
        prefix = (
            "🟢 Районная смена началась\n\n"
            "🔎 Ищу свежие новости по районам. Карточки появятся, когда найдутся подходящие поводы.\n\n"
            if active
            else "⚪ Районная смена завершена\n\n"
        )
        max_client.send_message(
            chat_id,
            prefix + render_district_control_status(include_summary=False),
            buttons=max_client.district_control_buttons(get_district_publish_profile()),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("district_control_transition_notify_failed", error=str(e))


def _cleanup_after_shift() -> None:
    """Освободить кэш завершённых постов после закрытия смены.

    cleanup_old сохраняет всё, что ещё требует решения модератора, поэтому фото
    карточек из очереди не пропадут до следующего рабочего дня.
    """
    removed = cleanup_old(days=0)
    log.info("shift_media_cleanup", removed=removed)


def _is_schedule_transition(
    previous_active: bool,
    previous_scheduled_active: bool,
    active: bool,
    scheduled_active: bool,
) -> bool:
    """Отличить границу расписания от ручного переключения ON/OFF/AUTO."""
    return previous_active != active and previous_scheduled_active != scheduled_active


def _control_tick() -> None:
    global _last_active, _last_scheduled_active
    global _last_district_active, _last_district_scheduled_active
    state = get_pipeline_state()
    district_state = get_district_pipeline_state()
    if _last_active is None or _last_scheduled_active is None:
        _last_active = state.active
        _last_scheduled_active = state.scheduled_active
        _last_district_active = district_state.active
        _last_district_scheduled_active = district_state.scheduled_active
        log.info("duty_state_initialized", active=state.active, mode=state.mode)
        return

    active_changed = state.active != _last_active
    schedule_changed = _is_schedule_transition(
        _last_active,
        _last_scheduled_active,
        state.active,
        state.scheduled_active,
    )
    _last_active = state.active
    _last_scheduled_active = state.scheduled_active
    if active_changed:
        log.info("duty_state_changed", active=state.active, mode=state.mode)
        if schedule_changed:
            _notify_control_transition(state.active)
            if not state.active:
                _cleanup_after_shift()
        if state.active:
            # Не ждём до 20 минут после включения: сразу собираем свежие публикации.
            _collection_job()

    district_active_changed = district_state.active != _last_district_active
    district_schedule_changed = _is_schedule_transition(
        bool(_last_district_active),
        bool(_last_district_scheduled_active),
        district_state.active,
        district_state.scheduled_active,
    )
    _last_district_active = district_state.active
    _last_district_scheduled_active = district_state.scheduled_active
    if district_active_changed:
        log.info("district_duty_state_changed", active=district_state.active, mode=district_state.mode)
        if district_schedule_changed:
            _notify_district_control_transition(district_state.active)
            if not district_state.active:
                _cleanup_after_shift()
        if district_state.active:
            _collection_job()


def build_scheduler() -> BlockingScheduler:
    proj = get_settings().project
    sched = BlockingScheduler(timezone="UTC")

    # Одна задача сбора со всех источников (внутри — параллельный fetch, последовательная
    # запись). Отдельные задачи на источник давали конкурентные записи и 'database is locked'.
    now = datetime.now(timezone.utc)
    intervals = [s.poll_interval_min for s in proj.sources if s.enabled]
    collect_interval = min(intervals) if intervals else 20
    # next_run_time -> первый запуск вскоре после старта (не ждём полный интервал).
    sched.add_job(_collection_job, "interval", minutes=max(1, collect_interval),
                  id="collect", max_instances=1, coalesce=True,
                  next_run_time=now + timedelta(seconds=5))

    sched.add_job(_processing_job, "interval", minutes=PROCESS_INTERVAL_MIN, id="processing",
                  max_instances=1, coalesce=True,
                  next_run_time=now + timedelta(seconds=45))
    sched.add_job(
        _publish_job,
        "interval",
        minutes=max(1, proj.publish.min_minutes_between_posts),
        id="publish",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        _control_tick,
        "interval",
        minutes=1,
        id="duty_control",
        max_instances=1,
        coalesce=True,
        next_run_time=now,
    )
    return sched


def main() -> None:
    configure_logging()
    init_db()
    sched = build_scheduler()
    log.info("scheduler_start", jobs=[j.id for j in sched.get_jobs()])
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler_stop")
