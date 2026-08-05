"""Инициализация движка и управление сессиями SQLAlchemy."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from aialarm.config import get_settings
from aialarm.db.models import Base

_engine = None
_SessionFactory: sessionmaker | None = None


def _get_factory() -> sessionmaker:
    global _engine, _SessionFactory
    if _SessionFactory is None:
        url = get_settings().secrets.database_url
        is_sqlite = url.startswith("sqlite")
        connect_args = {"check_same_thread": False} if is_sqlite else {}
        _engine = create_engine(url, future=True, connect_args=connect_args)
        if is_sqlite:
            # WAL + busy_timeout: планировщик и бот пишут в БД параллельно без блокировок.
            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _rec):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=15000")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _SessionFactory


def init_db() -> None:
    """Создать таблицы (пилот). В проде — Alembic-миграции."""
    _get_factory()
    Base.metadata.create_all(_engine)
    # create_all не добавляет колонки в существующую SQLite. Небольшие миграции
    # для пилота; старое image_url остаётся для совместимости.
    if _engine.dialect.name == "sqlite":
        with _engine.begin() as connection:
            raw_columns = {column["name"] for column in inspect(_engine).get_columns("raw_news")}
            if "image_urls" not in raw_columns:
                connection.exec_driver_sql("ALTER TABLE raw_news ADD COLUMN image_urls JSON")
            if "is_district_source" not in raw_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE raw_news ADD COLUMN is_district_source BOOLEAN DEFAULT 0"
                )
            control_columns = {
                column["name"] for column in inspect(_engine).get_columns("pipeline_control")
            }
            if "publish_profile" not in control_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE pipeline_control ADD COLUMN publish_profile VARCHAR(16) DEFAULT 'test'"
                )
            if "district_publish_profile" not in control_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE pipeline_control ADD COLUMN district_publish_profile VARCHAR(16) DEFAULT 'test'"
                )
            if "district_mode" not in control_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE pipeline_control ADD COLUMN district_mode VARCHAR(16) DEFAULT 'auto'"
                )
            if "district_override_started_at" not in control_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE pipeline_control ADD COLUMN district_override_started_at DATETIME"
                )
            if "district_override_until" not in control_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE pipeline_control ADD COLUMN district_override_until DATETIME"
                )
            post_columns = {
                column["name"] for column in inspect(_engine).get_columns("rewritten_posts")
            }
            if "publish_profile" not in post_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE rewritten_posts ADD COLUMN publish_profile VARCHAR(16) DEFAULT ''"
                )


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
