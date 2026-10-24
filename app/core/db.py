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
_lock = threading.RLock()


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
    # 先把 engine 建好（它内部有自己的锁），再进来建 sessionmaker，
    # 避免持锁期间再次抢同一把锁把自己锁死。
    engine = get_engine()
    with _lock:
        if _SessionLocal is not None:
            return _SessionLocal
        _SessionLocal = sessionmaker(
            bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True
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


# 新加的列：老库靠这里补上，否则升级后一写就报 no such column。
# create_all 只建表不改表，加字段必须手工登记。
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "links": {
        # 开放模式下访客回看自己那条链接用的一次性领取码
        "claim_hash": "VARCHAR(64)",
    },
    "click_events": {
        "device": "VARCHAR(16)",
        "blocked": "BOOLEAN NOT NULL DEFAULT 0",
        "platform": "VARCHAR(16)",
    },
}


def _ensure_columns() -> None:
    """给已有表补新列（幂等）。只做加列，绝不改类型或删列。"""
    from sqlalchemy import inspect, text

    engine = get_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name in present:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                logger.info("已为 %s 补列 %s", table, name)
        if "click_events" in existing_tables:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_click_events_platform ON click_events (platform)"))


def init_db() -> None:
    """建表 + 补列（幂等）。"""
    from app import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(bind=get_engine())
    _ensure_columns()
    logger.info("数据库已就绪")
