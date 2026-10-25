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


# 会话里的用户名就是权限标识：`owner` 是令牌级会话，其余一律按普通管理员算
OWNER = "owner"


def current_tier(request: Request) -> str | None:
    """当前会话的权限层级，未登录返回 None。

    为什么要有这一层：`REQUIRE_API_KEY` 打开后，`/api/*` 只认 API 密钥，
    而 `/api/admin/*` 压根不校验它——拿一把开出来的密钥就能直接读仪表盘、
    存储统计、全站数据。API 密钥的定位是「给脚本建短链」，不该等价于
    「管理员登录」。改成两把钥匙分工：密钥管接口，会话管后台，谁也不替谁。
    """
    name = current_admin(request)
    if name is None:
        return None
    from app.core.access import TIER_ADMIN, TIER_OWNER

    return TIER_OWNER if name == OWNER else TIER_ADMIN


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
