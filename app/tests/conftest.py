"""测试公共夹具：独立内存库 + TestClient。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ADMIN_TOKEN"] = "test-token"


@pytest.fixture(autouse=True)
def _isolate_clicks():
    """每个用例跑完清空延迟计数缓冲，避免串味。"""
    from app.core.clicks import click_buffer

    click_buffer.reset()
    yield
    click_buffer.reset()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.core import db as db_module
    from app.core.clicks import click_buffer
    from app.main import app

    db_module._engine = None
    db_module._SessionLocal = None
    click_buffer.reset()
    with TestClient(app) as c:
        yield c
    click_buffer.reset()
    db_module._engine = None
    db_module._SessionLocal = None


@pytest.fixture()
def flush_clicks():
    """把延迟计数缓冲立即结算，供断言点击数用。"""
    from app.core.clicks import flush_now

    def _run() -> int:
        return flush_now()

    return _run


@pytest.fixture()
def db():
    from sqlalchemy.orm import Session

    from app.core.db import get_sessionmaker, init_db

    init_db()
    sm = get_sessionmaker()
    session = sm()
    try:
        yield session
    finally:
        session.close()
