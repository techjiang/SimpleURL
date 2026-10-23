"""后台认证：令牌签名会话。

不引额外依赖，用 hmac + sha256 签一个「用户名:过期时间」的 cookie，
带 HttpOnly。改 SECRET_KEY 即可全站踢下线。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

from fastapi import Request

from app.core.config import settings

logger = logging.getLogger("simpleurl")

COOKIE_NAME = "su_admin"


def _sign(payload: str) -> str:
    return hmac.new(settings.session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(username: str = "admin", ttl: int | None = None) -> str:
    ttl = ttl or settings.session_ttl
    payload = f"{username}:{int(time.time()) + ttl}"
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    payload, _, sig = token.rpartition(".")
    if not hmac.compare_digest(_sign(payload), sig):
        return None
    username, _, expiry = payload.partition(":")
    try:
        if int(expiry) < int(time.time()):
            return None
    except ValueError:
        return None
    return username or None


def check_credentials(username: str, password: str) -> bool:
    """默认账号密码取 ADMIN_TOKEN，格式 admin:password 可分别设置。"""
    token = settings.admin_token
    if not token:
        return False
    if ":" in token:
        user, _, pwd = token.partition(":")
        return hmac.compare_digest(username, user) and hmac.compare_digest(password, pwd)
    return hmac.compare_digest(username, "admin") and hmac.compare_digest(password, token)


def current_admin(request: Request) -> str | None:
    return verify_token(request.cookies.get(COOKIE_NAME))


def is_admin_enabled() -> bool:
    return bool(settings.admin_token)


def login_response_value(token: str) -> dict:
    return {COOKIE_NAME: token}


def clear_cookie_value() -> dict:
    return {COOKIE_NAME: ""}
