"""Офлайн-проверки сменного режима редакции."""
from __future__ import annotations

from datetime import datetime, timezone

from aialarm.config import DutyScheduleCfg
from aialarm.control.schedule import resolve_schedule
from aialarm.pipeline.runner import _in_collection_window


CFG = DutyScheduleCfg(
    timezone="Europe/Moscow",
    weekday_start="17:00",
    weekday_end="23:00",
    weekend_start="09:00",
    weekend_end="23:00",
)


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def test_weekday_window_moscow():
    # 27 июля 2026 — понедельник; МСК = UTC+3.
    assert not resolve_schedule(_utc("2026-07-27T13:59:00"), CFG).active
    active = resolve_schedule(_utc("2026-07-27T14:00:00"), CFG)
    assert active.active
    assert active.start_utc == _utc("2026-07-27T14:00:00")
    assert active.end_utc == _utc("2026-07-27T20:00:00")
    assert not resolve_schedule(_utc("2026-07-27T20:00:00"), CFG).active


def test_weekend_window_moscow():
    # 1 августа 2026 — суббота.
    assert not resolve_schedule(_utc("2026-08-01T05:59:00"), CFG).active
    active = resolve_schedule(_utc("2026-08-01T06:00:00"), CFG)
    assert active.active
    assert active.end_utc == _utc("2026-08-01T20:00:00")
    assert not resolve_schedule(_utc("2026-08-01T20:00:00"), CFG).active


def test_next_transition():
    before_shift = resolve_schedule(_utc("2026-07-27T10:00:00"), CFG)
    assert before_shift.next_transition_utc == _utc("2026-07-27T14:00:00")
    during_shift = resolve_schedule(_utc("2026-07-27T18:00:00"), CFG)
    assert during_shift.next_transition_utc == _utc("2026-07-27T20:00:00")


def test_collection_window_uses_publication_time():
    since = _utc("2026-07-27T14:00:00")
    assert not _in_collection_window(_utc("2026-07-27T13:59:59"), since)
    assert _in_collection_window(_utc("2026-07-27T14:00:00"), since)
    assert _in_collection_window(None, since)
