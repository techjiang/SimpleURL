"""数据库连接与初始化。"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings

logger = logging.getLogger("simpleurl")

_engine = None
_SessionLocal: sessionmaker[Session] | None = None
_lock = threading.Lock()


class Base(DeclarativeBase):
    """所有模型的基类。"""


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def get_engine():
    """全局唯一 Engine（带 SQLite 并发优化）。"""
    global _engine
    if _engine is not None:
        return _engine
    with _lock:
        if _engine is not None:
            return _engine
        url = settings.database_url
        kwargs: dict = {"future": True, "echo": False}
        if _is_sqlite(url):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
            if url in {"sqlite://", "sqlite:///:memory:"}:
                kwargs["poolclass"] = StaticPool
        engine = create_engine(url, **kwargs)
        if _is_sqlite(url):
            @event.listens_for(engine, "connect")
            def _sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        _engine = engine
        return engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is not None:
        return _SessionLocal
    with _lock:
        if _SessionLocal is not None:
            return _SessionLocal
        _SessionLocal = sessionmaker(
            bind=get_engine(), autoflush=False, autocommit=False, expire_on_commit=False, future=True
        )
        return _SessionLocal


@contextmanager
def session_scope():
    """事务性 session 上下文：异常回滚，正常提交。"""
    sm = get_sessionmaker()
    session = sm()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db():
    """FastAPI 依赖：请求级 session。"""
    sm = get_sessionmaker()
    session = sm()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """建表（幂等）。"""
    from app import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(bind=get_engine())
    logger.info("数据库已就绪")
