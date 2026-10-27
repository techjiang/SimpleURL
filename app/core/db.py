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
    """建表（幂等），并给老库补上后加的列。"""
    from app import models  # noqa: F401  确保模型已注册

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _add_missing_columns(engine)
    logger.info("数据库已就绪")


def _add_missing_columns(engine) -> None:
    """轻量自愈：只补列，不改列、不删列。

    项目坚持无迁移框架（单文件部署的卖点），但新版本要能给老库加字段——
    否则升级后第一次写库就炸。SQLite / Postgres 的 ALTER TABLE ADD COLUMN
    语法一致，够用；真要做结构调整还是得手工上迁移。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in have:
                continue
            ddl = _add_column_sql(engine, table.name, column)
            if not ddl:
                continue
            with engine.begin() as conn:
                conn.execute(text(ddl))
            logger.info("已为 %s 补充列 %s", table.name, column.name)


def _add_column_sql(engine, table: str, column) -> str | None:
    try:
        col_type = column.type.compile(dialect=engine.dialect)
    except Exception:  # pragma: no cover - 极少见的自定义类型
        return None
    parts = [f'ALTER TABLE "{table}" ADD COLUMN "{column.name}" {col_type}']
    default = column.server_default
    if default is not None:
        arg = getattr(default, "arg", None)
        if isinstance(arg, str):
            parts.append(f"DEFAULT {arg}")
    # 老行需要一个值才满足 NOT NULL：数值列补 0，其余交给默认值
    if not column.nullable and default is None:
        if col_type.upper().startswith(("INTEGER", "BIGINT", "SMALLINT", "FLOAT", "NUMERIC")):
            parts.append("DEFAULT 0")
    return " ".join(parts)
