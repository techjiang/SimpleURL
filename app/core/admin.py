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


def token_credentials() -> tuple[str, str] | None:
    """ADMIN_TOKEN 也能当账号密码：`admin:password` 分开写，纯口令则账号固定 admin。

    安装脚本前几版只写 ADMIN_TOKEN，这条兼容分支扔不掉——升个版本，
    老部署的后台就进不去了。
    """
    token = settings.admin_token
    if not token:
        return None
    if ":" in token:
        user, _, pwd = token.partition(":")
        return user, pwd
    return "admin", token


def _same_text(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").strip().encode(), (b or "").strip().encode())


def check_credentials(username: str, password: str) -> bool:
    """校验后台账号密码。

    两个来源都算（ADMIN_USERNAME/ADMIN_PASSWORD 与 ADMIN_TOKEN），
    四条比对全都走一遍再合结论：不因为「先撞上哪一条」而改变耗时，
    也不给外面一个「账号对不对」的额外信号。
    账号名大小写不敏感——安装时抄成 Admin，不该把用户自己关在门外。
    """
    if not is_admin_enabled():
        return False
    name = (username or "").strip().lower()
    pwd = password or ""
    pair = token_credentials()

    user_ok = pair is not None and _same_text(name, pair[0].lower())
    pass_ok = pair is not None and _same_text(pwd, pair[1])
    if settings.admin_password:
        user_ok = _same_text(name, settings.admin_username.lower()) or user_ok
        pass_ok = _same_text(pwd, settings.admin_password) or pass_ok
    return user_ok and pass_ok


def current_admin(request: Request) -> str | None:
    return verify_token(request.cookies.get(COOKIE_NAME))


def is_admin_enabled() -> bool:
    return bool(settings.admin_token or settings.admin_password)


def login_response_value(token: str) -> dict:
    return {COOKIE_NAME: token}


def clear_cookie_value() -> dict:
    return {COOKIE_NAME: ""}


# ---------------------------------------------------------------- 凭据改写
def env_file_path():
    """要写回的 .env 路径。

    显式配了 SIMPLEURL_ENV_FILE 就用它（Docker 里指 /data/.env）；
    否则取项目根：app/core/admin.py 往上两层。别拿 static_dir 之类去数层级，
    数错一级就会把密码写进无关目录。
    """
    import os
    from pathlib import Path

    override = os.getenv("SIMPLEURL_ENV_FILE", "").strip()
    if override:
        return Path(override)
    from app.core import config as config_module

    return Path(config_module.__file__).resolve().parents[2] / ".env"


def write_env_vars(pairs: dict[str, str]) -> bool:
    """就地改写 .env 里的几行：有则替换、无则追加，其余行一字不动。

    返回是否写成功。写不进去不是错误——容器只读挂载、目录没权限都会这样，
    当前进程照样能用新密码，只是重启后会退回去。这一点得让调用方说清楚，
    比装作成功要好。
    """
    path = env_file_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        for key, value in pairs.items():
            prefix = f"{key}="
            for i, line in enumerate(lines):
                if line.strip().startswith(prefix):
                    lines[i] = f"{key}={value}"
                    break
            else:
                lines.append(f"{key}={value}")
        path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)  # 里面是密码，别让它跟着 umask 漂
        except OSError:
            pass
        return True
    except OSError as exc:  # pragma: no cover - 权限问题只在个别部署出现
        logger.warning("写回 .env 失败：%s", exc)
        return False
