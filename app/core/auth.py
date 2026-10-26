"""访问控制：公开 / 管理员邀请 / 仅管理员 三种模式。

- `public`：谁都能创建链接，管理操作（改、删、列表、统计）需要管理员
- `admin`：只有管理员能创建和管理，访客只能跳转
- `invite`：访客凭邀请码创建，邀请码由管理员签发，管理操作仍归管理员

密钥校验全部走 Argon2id（见 `crypto.py`）。配置里既可以写明文密钥，
也可以直接写 Argon2id 哈希串——后者即使配置文件泄露也拿不回原密钥。
会话令牌用独立的 `SECRET_KEY` 签名，不依赖管理密钥本身。
"""

from __future__ import annotations

import time

from fastapi import Depends, Request

from app.core import crypto, runtime
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
_SESSION_TOKEN_PREFIX = "su1"


class AuthError(Exception):
    """鉴权失败，code 为 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.message = message
        self.status = status


def _sign(value: str) -> str:
    """会话签名：用 SECRET_KEY，不用管理密钥。"""
    return crypto.sign(settings.secret_key, value)


def _idle_ttl() -> int:
    """会话闲置超时，且不长于 Cookie 总有效期。"""
    return max(60, min(int(settings.session_idle_timeout), COOKIE_MAX_AGE))


def _issued(value: str, ttl: int, now: int) -> str:
    return _sign(f"{_SESSION_TOKEN_PREFIX}:{value}:{now}:{ttl}")


def _token(value: str, ttl: int, issued_at: int | None = None) -> str:
    """由密钥派生、带时间戳的签名令牌，无需服务端存会话。

    - 时间戳里编了「签发时刻」，于是同一秒内多次签发的令牌完全相同——
      浏览器里的 Cookie 不会因为刷新页面就变一次。
    - 有效期 ttl 一并签进令牌：否则签发时声明的短有效期，
      校验方可以拿一个更长的默认值把它「救活」。
    - issued_at 用于续期：令牌原样重签，但把「最近活跃」推到现在。
    """
    ts = int(time.time()) if issued_at is None else int(issued_at)
    return f"{ts}.{_issued(value, ttl, ts)}"


def _token_fresh(token: str | None, value: str, ttl: int) -> bool:
    """令牌本身有效（签名 + 签发时未过期），且最近 ttl 秒内活跃过。"""
    if not token or "." not in token:
        return False
    ts, sig = token.split(".", 1)
    if not ts.isdigit():
        return False
    # ttl 既参与签名也参与过期判断，改任一边都验不过
    if int(ts) + ttl < int(time.time()):
        return False
    return crypto.compare_digest(sig, _issued(value, ttl, int(ts)))


def _token_valid(token: str | None, value: str, ttl: int) -> bool:
    """校验令牌，并按闲置超时收紧。

    Cookie 的 7 天是「最迟什么时候彻底失效」，这里再叠一层闲置超时：
    公用电脑上签完就走的会话不该一直活着。令牌每次校验通过后会被重签
    （见 `refresh_session`），所以持续使用不会突然掉线。
    """
    if not _token_fresh(token, value, ttl):
        return False
    try:
        issued = int(token.split(".", 1)[0])
    except (ValueError, AttributeError):
        return False
    return issued + _idle_ttl() >= int(time.time())


def refresh_session(token: str | None) -> str | None:
    """闲置时间过半就发一张新的，返回需要写回 Cookie 的令牌，否则 None。"""
    if not token or "." not in token:
        return None
    ts = token.split(".", 1)[0]
    if not ts.isdigit():
        return None
    issued = int(ts)
    now = int(time.time())
    if now - issued < max(60, _idle_ttl() // 2):
        return None
    return _token(f"admin::{_admin_epoch()}", COOKIE_MAX_AGE, issued_at=now)


def _secret_matches(secret: str | None) -> bool:
    """校验管理密钥。支持明文与 Argon2id 哈希两种配置形式。"""
    if not secret or not settings.admin_token:
        return False
    configured = settings.admin_token
    if crypto.is_hash(configured):
        return crypto.verify_secret(secret, configured)
    # 兼容明文配置；比对用恒定时间，防时序侧信道
    return crypto.compare_digest(secret.strip(), configured)


def admin_token() -> str:
    return settings.admin_token


def admin_enabled() -> bool:
    return bool(settings.admin_token)


def _admin_epoch() -> str:
    """管理员会话的「代际」标识。

    会话令牌只带 SECRET_KEY 签名，这里把当前管理密钥的指纹也编进签名内容，
    换来两个性质：
    - 轮换 / 停用 ADMIN_TOKEN 时，旧 Cookie 立刻全部失效（不需要维护黑名单）
    - 令牌不会泄露管理密钥本身
    """
    if not settings.admin_token:
        return "disabled"
    # 只取指纹，避免把密钥明文编进签名内容
    return crypto.fingerprint(settings.admin_token)


def admin_session(ttl: int = COOKIE_MAX_AGE) -> str:
    return _token(f"admin::{_admin_epoch()}", ttl)


def is_admin_token(token: str | None) -> bool:
    """是否为本实例、当前密钥下签发的有效管理员会话。"""
    if not settings.admin_token:
        return False
    return _token_valid(token, f"admin::{_admin_epoch()}", COOKIE_MAX_AGE)


def admin_password_ok(password: str | None) -> bool:
    return _secret_matches(password)


def invite_key() -> str:
    """当前邀请码（随管理密钥与日期滚动）。"""
    day = time.strftime("%Y-%m-%d")
    return _sign(f"invite::{day}")[:16]


def invite_session(ttl: int = COOKIE_MAX_AGE) -> str:
    """邀请码解锁后写入 Cookie 的会话令牌。"""
    return _token(f"invite::{_admin_epoch()}::{invite_key()}", ttl)


def invite_session_ok(token: str | None) -> bool:
    if not settings.admin_token:
        return False
    return _token_valid(token, f"invite::{_admin_epoch()}::{invite_key()}", COOKIE_MAX_AGE)


def invite_ok(code: str | None) -> bool:
    if not code or not settings.admin_token:
        return False
    return crypto.compare_digest(code.strip().lower(), invite_key())


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


def current_mode() -> str:
    """当前生效的访问模式：后台配置优先，其次环境变量。"""
    mode = runtime.get_str("access_mode") or settings.access_mode
    return mode if mode in ACCESS_MODES else MODE_PUBLIC


def resolve_access(request: Request) -> Access:
    mode = current_mode()
    is_admin = is_admin_token(_cookie(request, ADMIN_COOKIE))

    if mode == MODE_PUBLIC:
        return Access(mode, is_admin, can_create=True)

    # admin / invite 模式下没配密钥：退回开放，但**不能**因为「没有管理员」就把
    # 持有旧 Cookie 的人判成管理员。密钥撤了就该彻底失效，否则撤钥反而不降权。
    if not admin_enabled():
        return Access(MODE_PUBLIC, False, can_create=True,
                      reason="未配置 ADMIN_TOKEN，已自动切换为开放模式")

    if mode == MODE_ADMIN:
        return Access(mode, is_admin, can_create=is_admin,
                      reason="" if is_admin else "站点当前为管理员模式，创建链接需先登录")

    # invite
    has_key = invite_session_ok(_cookie(request, INVITE_COOKIE))
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


# ---- API 密钥 ----
API_KEY_HEADER = "X-Api-Key"


def _hash_api_key(raw: str) -> str:
    return crypto.sign(settings.secret_key, f"apikey::{raw}")


def create_api_key(db, name: str) -> tuple[str, object]:
    """返回 (明文密钥, 记录)。明文只在这一刻存在，之后只能看前缀。"""
    from app.models import ApiKey

    raw = "sk_" + crypto.random_token(24)
    record = ApiKey(name=(name or "未命名")[:64], prefix=raw[:10], key_hash=_hash_api_key(raw))
    db.add(record)
    db.flush()
    return raw, record


def verify_api_key(db, raw: str | None) -> bool:
    """校验 API 密钥；命中就顺手记一次使用时间。"""
    if not raw:
        return False
    from sqlalchemy import select

    from app.models import ApiKey
    from app.core.timeutil import utcnow

    digest = _hash_api_key(raw.strip())
    row = db.execute(select(ApiKey).where(ApiKey.revoked.is_(False))).scalars().all()
    for record in row:
        if crypto.compare_digest(record.key_hash, digest):
            record.last_used_at = utcnow()
            db.flush()
            return True
    return False


def api_key_required() -> bool:
    return runtime.get_bool("api_key_required")


# ---- 登录 / 口令验证限速 ----
_login_hits: dict[str, list[float]] = {}
_LOGIN_SWEEP_EVERY = 300
_login_last_sweep = [0.0]


def _sweep_login_hits(now: float) -> None:
    """每 5 分钟清一次过期窗口。

    不清的话这张表只涨不落：扫一遍不同来源就能把内存顶上去，而限速器
    本身反而成了拒绝服务的入口。
    """
    if now - _login_last_sweep[0] < _LOGIN_SWEEP_EVERY:
        return
    _login_last_sweep[0] = now
    for key in [k for k, v in _login_hits.items() if not v or now - v[-1] > 3600]:
        _login_hits.pop(key, None)


def login_allowed(request: Request, limit: int = 10, window: int = 300) -> bool:
    # 与失败计数分开命名空间，否则同一份桶被两边共用，判据全是错的
    key = "login:" + (client_ip(request) or "unknown")
    now = time.monotonic()
    bucket = [t for t in _login_hits.get(key, []) if now - t < window]
    _sweep_login_hits(now)
    if len(bucket) >= limit:
        _login_hits[key] = bucket
        return False
    bucket.append(now)
    _login_hits[key] = bucket
    return True


FAIL_KEY = "fail::{scope}"


def _fail_key(request: Request, scope: str) -> str:
    # 与登录限速分开记：错 9 次成功 1 次会把登录窗口刷空，失败次数不会
    return f"fail:{scope}:{client_ip(request) or 'unknown'}"


def note_login_failure(request: Request, scope: str = "admin") -> None:
    _login_hits.setdefault(_fail_key(request, scope), []).append(time.monotonic())


def too_many_failures(request: Request, limit: int = 20, window: int = 900, scope: str = "admin") -> bool:
    key = _fail_key(request, scope)
    now = time.monotonic()
    bucket = [t for t in _login_hits.get(key, []) if now - t < window]
    _login_hits[key] = bucket
    return len(bucket) >= limit
