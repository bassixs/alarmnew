"""Инициализация движка и управление сессиями SQLAlchemy."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from aialarm.config import get_settings
from aialarm.db.models import Base

_engine = None
_SessionFactory: sessionmaker | None = None


def _get_factory() -> sessionmaker:
    global _engine, _SessionFactory
    if _SessionFactory is None:
        url = get_settings().secrets.database_url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, future=True, connect_args=connect_args)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _SessionFactory


def init_db() -> None:
    """Создать таблицы (пилот). В проде — Alembic-миграции."""
    _get_factory()
    Base.metadata.create_all(_engine)


def get_session() -> Session:
    return _get_factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
