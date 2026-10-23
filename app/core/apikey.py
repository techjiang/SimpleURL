"""API 密钥校验：开启 REQUIRE_API_KEY 后，/api/* 必须带密钥。"""

from __future__ import annotations

import logging

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.settings_store import effective
from app.core.timeutil import utcnow
from app.models import ApiKey

logger = logging.getLogger("simpleurl")

HEADER = "X-Api-Key"


def extract_key(request: Request) -> str | None:
    key = request.headers.get(HEADER) or request.query_params.get("key")
    return key.strip() if key else None


def verify(request: Request, db: Session) -> bool:
    if not effective(db, "require_api_key"):
        return True
    key = extract_key(request)
    if not key:
        return False
    row = db.execute(select(ApiKey.id).where(ApiKey.key == key, ApiKey.enabled.is_(True))).first()
    return row is not None


def touch(db: Session, key: str | None) -> None:
    if not key:
        return
    row = db.execute(select(ApiKey).where(ApiKey.key == key)).scalar_one_or_none()
    if row is not None:
        row.last_used_at = utcnow()
        db.flush()
