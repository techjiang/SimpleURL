"""访问控制：公开 / 管理员邀请 / 仅管理员 三种模式。

- `public`：谁都能创建链接，管理操作（改、删、列表、统计）需要管理员
- `admin`：只有管理员能创建和管理，访客只能跳转
- `invite`：访客凭邀请码创建，邀请码由管理员签发，管理操作仍归管理员
"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Depends, Request

from app.core.config import settings
from app.core.security import client_ip

# ---- 模式 ----
MODE_PUBLIC = "public"
MODE_ADMIN = "admin"
MODE_INVITE = "invite"
ACCESS_MODES = (MODE_PUBLIC, MODE_ADMIN, MODE_INVITE)
MODE_LABELS = {
    MODE_PUBLIC: "开放外部可使用",
    MODE_ADMIN: "仅管理员可使用",
    MODE_INVITE: "邀请码可使用",
}

# ---- Cookie / Header ----
ADMIN_COOKIE = "su_admin"
INVITE_COOKIE = "su_invite_key"
COOKIE_MAX_AGE = 7 * 24 * 3600


class AuthError(Exception):
    """鉴权失败，code 为 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.message = message
        self.status = status


def _sign(value: str, secret: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def _token(secret: str, ttl: int) -> str:
    """由密钥派生、带时间戳的签名令牌，无需服务端存会话。"""
    ts = str(int(time.time()))
    return f"{ts}.{_sign(ts, secret)[:32]}"


def _token_valid(token: str | None, secret: str, ttl: int) -> bool:
    if not token or not secret or "." not in token:
        return False
    ts, sig = token.split(".", 1)
    if not ts.isdigit():
        return False
    if int(ts) + ttl < int(time.time()):
        return False
    return hmac.compare_digest(sig, _sign(ts, secret)[:32])


def admin_token() -> str:
    return settings.admin_token


def admin_enabled() -> bool:
    return bool(settings.admin_token)


def admin_session(ttl: int = COOKIE_MAX_AGE) -> str:
    return _token(settings.admin_token, ttl)


def is_admin_token(token: str | None) -> bool:
    """是否为本实例签发的有效管理员会话。"""
    return _token_valid(token, settings.admin_token, COOKIE_MAX_AGE)


def admin_password_ok(password: str | None) -> bool:
    if not password or not settings.admin_token:
        return False
    return hmac.compare_digest(password, settings.admin_token)


def invite_key(ttl: int = COOKIE_MAX_AGE) -> str:
    """当前邀请码（随管理密钥与日期滚动）。"""
    day = time.strftime("%Y-%m-%d")
    return _sign(f"invite::{day}", settings.admin_token)[:16]


def invite_ok(code: str | None) -> bool:
    if not code or not settings.admin_token:
        return False
    return hmac.compare_digest(code.strip().lower(), invite_key())


# ---- 每次请求的判定 ----
class Access:
    """一次请求的身份快照：是否管理员、是否允许创建。"""

    __slots__ = ("mode", "is_admin", "can_create", "reason")

    def __init__(self, mode: str, is_admin: bool, can_create: bool, reason: str = ""):
        self.mode = mode
        self.is_admin = is_admin
        self.can_create = can_create
        self.reason = reason

    @property
    def mode_label(self) -> str:
        return MODE_LABELS.get(self.mode, self.mode)

    @property
    def admin_login_enabled(self) -> bool:
        return admin_enabled()

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "mode_label": self.mode_label,
            "is_admin": self.is_admin,
            "can_create": self.can_create,
            "reason": self.reason,
            "admin_login_enabled": self.admin_login_enabled,
            "invite_required": self.mode == MODE_INVITE and not self.is_admin,
        }


def _cookie(request: Request, name: str) -> str | None:
    return request.cookies.get(name)


def resolve_access(request: Request) -> Access:
    mode = settings.access_mode
    is_admin = is_admin_token(_cookie(request, ADMIN_COOKIE))

    if mode == MODE_PUBLIC:
        return Access(mode, is_admin, can_create=True)

    if mode == MODE_ADMIN:
        if not admin_enabled():
            # 没设管理密钥就退回公开，避免整站被锁死
            return Access(MODE_PUBLIC, True, can_create=True, reason="未配置 ADMIN_TOKEN，已自动切换为开放模式")
        return Access(mode, is_admin, can_create=is_admin,
                      reason="" if is_admin else "站点当前为管理员模式，创建链接需先登录")

    # invite
    if not admin_enabled():
        return Access(MODE_PUBLIC, True, can_create=True, reason="未配置 ADMIN_TOKEN，已自动切换为开放模式")
    has_key = _token_valid(_cookie(request, INVITE_COOKIE), f"invite::{invite_key()}", COOKIE_MAX_AGE)
    if is_admin:
        return Access(mode, True, can_create=True)
    return Access(mode, False, can_create=has_key,
                  reason="" if has_key else "需要管理员签发的邀请码才能创建")


# ---- FastAPI 依赖 ----
def get_access(request: Request) -> Access:
    return resolve_access(request)


def require_admin(access: Access = Depends(get_access)) -> Access:
    if not access.is_admin:
        raise AuthError("仅管理员可访问", 401)
    return access


def require_create(access: Access = Depends(get_access)) -> Access:
    if not access.can_create:
        raise AuthError(access.reason or "当前不允许创建短链接", 403)
    return access


# ---- 简单的登录限速 ----
_login_hits: dict[str, list[float]] = {}


def login_allowed(request: Request, limit: int = 10, window: int = 300) -> bool:
    key = client_ip(request) or "unknown"
    now = time.monotonic()
    bucket = [t for t in _login_hits.get(key, []) if now - t < window]
    if len(bucket) >= limit:
        _login_hits[key] = bucket
        return False
    bucket.append(now)
    _login_hits[key] = bucket
    return True
