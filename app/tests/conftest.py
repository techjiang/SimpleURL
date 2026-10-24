"""测试公共夹具：独立内存库 + TestClient。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ADMIN_TOKEN"] = "test-token"
# 会话签名密钥必须稳定，否则每次 import 出的 Settings 都不一样，签名 Cookie 全部失效
os.environ["SECRET_KEY"] = "test-secret-key"


ADMIN_TOKEN = "test-token"


@pytest.fixture()
def client():
    """管理员客户端：模拟已登录，便于覆盖管理类接口。"""
    from fastapi.testclient import TestClient

    from app.core import auth, runtime
    from app.core import db as db_module
    from app.core.clickbuffer import click_buffer
    from app.core.config import settings
    from app.main import app

    settings.access_mode = "public"
    settings.admin_token = ADMIN_TOKEN
    auth._login_hits.clear()
    runtime.reset()  # 运行期配置可能被上一个用例改过，这里清成默认
    db_module._engine = None
    db_module._SessionLocal = None
    with TestClient(app) as c:
        click_buffer.reset()  # 启动时 lifespan 会结算一次，之后再清
        c.cookies.set(auth.ADMIN_COOKIE, auth.admin_session())
        yield c
    click_buffer.reset()
    db_module._engine = None
    db_module._SessionLocal = None


@pytest.fixture()
def guest():
    """访客客户端：不带任何身份。"""
    from fastapi.testclient import TestClient

    from app.core import db as db_module
    from app.core.config import settings
    from app.main import app

    from app.core import runtime
    from app.core.clickbuffer import click_buffer

    settings.access_mode = "public"
    settings.admin_token = ADMIN_TOKEN
    runtime.reset()
    db_module._engine = None
    db_module._SessionLocal = None
    with TestClient(app) as c:
        click_buffer.reset()
        yield c
    click_buffer.reset()
    db_module._engine = None
    db_module._SessionLocal = None


@pytest.fixture()
def cursor():
    """直接操作库的夹具：给页面会话与后台批量写共用同一个 session。"""
    from app.core.db import get_sessionmaker, init_db

    init_db()
    return get_sessionmaker()


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
