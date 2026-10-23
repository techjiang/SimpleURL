"""测试公共夹具：独立内存库 + TestClient。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ADMIN_TOKEN"] = "test-token"


ADMIN_TOKEN = "test-token"


@pytest.fixture()
def client():
    """管理员客户端：模拟已登录，便于覆盖管理类接口。"""
    from fastapi.testclient import TestClient

    from app.core import auth
    from app.core import db as db_module
    from app.core.config import settings
    from app.main import app

    settings.access_mode = "public"
    settings.admin_token = ADMIN_TOKEN
    auth._login_hits.clear()
    db_module._engine = None
    db_module._SessionLocal = None
    with TestClient(app) as c:
        c.cookies.set(auth.ADMIN_COOKIE, auth.admin_session())
        yield c
    db_module._engine = None
    db_module._SessionLocal = None


@pytest.fixture()
def guest():
    """访客客户端：不带任何身份。"""
    from fastapi.testclient import TestClient

    from app.core import db as db_module
    from app.core.config import settings
    from app.main import app

    settings.access_mode = "public"
    settings.admin_token = ADMIN_TOKEN
    db_module._engine = None
    db_module._SessionLocal = None
    with TestClient(app) as c:
        yield c
    db_module._engine = None
    db_module._SessionLocal = None


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
