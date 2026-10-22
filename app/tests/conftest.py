"""测试公共夹具：独立内存库 + TestClient。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ADMIN_TOKEN"] = "test-token"


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.core import db as db_module
    from app.main import app

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
