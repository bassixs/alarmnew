"""Сменный режим помощника редакции и ручные переопределения.

MAX-бот работает постоянно, а сбор/фильтрация/публикация включаются только в рабочее
окно. Ручные ON/OFF сохраняются в SQLite и действуют до ближайшей границы расписания,
после чего система сама возвращается в AUTO.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from aialarm.config import DutyScheduleCfg, get_settings
from aialarm.db import session_scope
from aialarm.db.models import PipelineControl


@dataclass(frozen=True, slots=True)
class ScheduleWindow:
    active: bool
    start_utc: datetime | None
    end_utc: datetime | None
    next_transition_utc: datetime | None


@dataclass(frozen=True, slots=True)
class EffectivePipelineState:
    active: bool
    mode: str
    scheduled_active: bool
    active_since: datetime | None
    override_until: datetime | None
    next_transition: datetime | None
    timezone_name: str


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clock(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Некорректное время смены: {value!r}; ожидается HH:MM") from exc


def _daily_window(day: date, cfg: DutyScheduleCfg, tz: ZoneInfo) -> tuple[datetime, datetime]:
    weekend = day.weekday() >= 5
    start_value = cfg.weekend_start if weekend else cfg.weekday_start
    end_value = cfg.weekend_end if weekend else cfg.weekday_end
    start = datetime.combine(day, _clock(start_value), tzinfo=tz)
    end = datetime.combine(day, _clock(end_value), tzinfo=tz)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def resolve_schedule(
    now: datetime | None = None,
    cfg: DutyScheduleCfg | None = None,
) -> ScheduleWindow:
    cfg = cfg or get_settings().project.duty_schedule
    now_utc = _utc(now or datetime.now(timezone.utc))
    if not cfg.enabled:
        return ScheduleWindow(True, now_utc, None, None)

    tz = ZoneInfo(cfg.timezone)
    local_now = now_utc.astimezone(tz)
    active_start: datetime | None = None
    active_end: datetime | None = None

    # Предыдущий день нужен для окон, пересекающих полночь.
    for offset in (-1, 0):
        start, end = _daily_window(local_now.date() + timedelta(days=offset), cfg, tz)
        if start <= local_now < end:
            active_start, active_end = start, end
            break

    transitions: list[datetime] = []
    for offset in range(-1, 9):
        start, end = _daily_window(local_now.date() + timedelta(days=offset), cfg, tz)
        transitions.extend(dt for dt in (start, end) if dt > local_now)
    next_transition = min(transitions) if transitions else None

    return ScheduleWindow(
        active=active_start is not None,
        start_utc=active_start.astimezone(timezone.utc) if active_start else None,
        end_utc=active_end.astimezone(timezone.utc) if active_end else None,
        next_transition_utc=(
            next_transition.astimezone(timezone.utc) if next_transition else None
        ),
    )


def _load_control() -> tuple[str, datetime | None, datetime | None]:
    with session_scope() as session:
        row = session.get(PipelineControl, 1)
        if row is None:
            row = PipelineControl(id=1)
            session.add(row)
            session.flush()
        return (
            (row.mode or "auto").lower(),
            _utc(row.override_started_at) if row.override_started_at else None,
            _utc(row.override_until) if row.override_until else None,
        )


def get_pipeline_state(now: datetime | None = None) -> EffectivePipelineState:
    now_utc = _utc(now or datetime.now(timezone.utc))
    cfg = get_settings().project.duty_schedule
    scheduled = resolve_schedule(now_utc, cfg)
    mode, override_started, override_until = _load_control()
    override_valid = mode in {"on", "off"} and bool(
        override_until and override_until > now_utc
    )

    if override_valid:
        active = mode == "on"
        if active and scheduled.active:
            active_since = scheduled.start_utc
        elif active:
            active_since = override_started or now_utc
        else:
            active_since = None
        effective_mode = mode
    else:
        active = scheduled.active
        active_since = scheduled.start_utc if active else None
        effective_mode = "auto"
        override_until = None

    return EffectivePipelineState(
        active=active,
        mode=effective_mode,
        scheduled_active=scheduled.active,
        active_since=active_since,
        override_until=override_until,
        next_transition=scheduled.next_transition_utc,
        timezone_name=cfg.timezone,
    )


def set_control_mode(mode: str, now: datetime | None = None) -> EffectivePipelineState:
    mode = mode.lower()
    if mode not in {"auto", "on", "off"}:
        raise ValueError(f"Неизвестный режим: {mode}")

    now_utc = _utc(now or datetime.now(timezone.utc))
    scheduled = resolve_schedule(now_utc)
    with session_scope() as session:
        row = session.get(PipelineControl, 1)
        if row is None:
            row = PipelineControl(id=1)
            session.add(row)
        row.mode = mode
        row.updated_at = now_utc
        if mode == "auto":
            row.override_started_at = None
            row.override_until = None
        else:
            row.override_started_at = now_utc
            row.override_until = scheduled.next_transition_utc or (now_utc + timedelta(days=1))
    return get_pipeline_state(now_utc)


def _local_hm(value: datetime | None, tz_name: str) -> str:
    if value is None:
        return "—"
    return _utc(value).astimezone(ZoneInfo(tz_name)).strftime("%d.%m %H:%M")


def render_control_status(state: EffectivePipelineState | None = None) -> str:
    state = state or get_pipeline_state()
    cfg = get_settings().project.duty_schedule
    mode_names = {"auto": "AUTO", "on": "ON вручную", "off": "OFF вручную"}
    lines = [
        f"🤖 Помощник: {'ВКЛЮЧЁН' if state.active else 'ВЫКЛЮЧЕН'}",
        f"Режим: {mode_names[state.mode]}",
        f"Часовой пояс: {state.timezone_name}",
        f"Будни: {cfg.weekday_start}–{cfg.weekday_end}",
        f"Выходные: {cfg.weekend_start}–{cfg.weekend_end}",
    ]
    if state.override_until:
        lines.append(f"Ручной режим до: {_local_hm(state.override_until, state.timezone_name)}")
    elif state.next_transition:
        transition = _local_hm(state.next_transition, state.timezone_name)
        lines.append(f"Следующее переключение: {transition}")
    return "\n".join(lines)
