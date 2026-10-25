"""站点访问模式：区分「开放外部可使用」与「仅管理员可使用」。

- open  ：默认。访客可创建；但链接库 / 详情 / 统计 / 导出仍只给管理员，
          刚建的那一条凭一次性回执（领取码）可以看
- admin ：访客只能跳转，创建与所有浏览操作都要求管理员会话

模式本身是运行参数（见 settings_store.EDITABLE），后台可随时改；
未配置 ADMIN_TOKEN 时自动退回 open，避免把站点锁死在门外。
"""

from __future__ import annotations

from fastapi import Request

from app.core.admin import current_admin, is_admin_enabled
from app.core.config import settings
from app.core.settings_store import effective

OPEN = "open"
ADMIN = "admin"
MODES = (OPEN, ADMIN)
LABELS = {OPEN: "开放外部可使用", ADMIN: "仅管理员可使用"}

# 普通管理员会话：能看到链接库、能改配置
TIER_ADMIN = "admin"
# 令牌级会话（`admin:<口令>` 形式的账号）：只在极端情况用来救场——
# 口令泄露后要能登录改口令、清 API 密钥，所以不给它读数据的权限，只给改配置
TIER_OWNER = "owner"


def normalize_mode(raw: object) -> str:
    mode = str(raw or "").strip().lower()
    return mode if mode in MODES else OPEN


# 令牌多长才算能上生产：低于这个长度等于门没锁
WEAK_TOKEN_LENGTH = 12


def current_mode(db=None) -> str:
    """当前生效模式。没配管理令牌时永远开放，防止配错锁死站点。

    口令过短（见 locked_by_weak_token）不在这里降级：模式照实报，
    但访客能力一律收回——界面得说真话，权限得按最坏情况给。
    """
    if not is_admin_enabled():
        return OPEN
    return normalize_mode(effective(db, "access_mode"))


def token_is_weak() -> bool:
    token = settings.admin_token
    if not token:
        return False
    # `admin:password` 形式只看口令部分，别被用户名凑了长度
    secret = token.partition(":")[2] if ":" in token else token
    return len(secret) < WEAK_TOKEN_LENGTH


def is_admin_mode(db=None) -> bool:
    return current_mode(db) == ADMIN


def is_admin(request: Request, db=None) -> bool:
    """当前请求是否已通过管理员登录。

    弱口令时顺手把创建与浏览一并关掉：登录框留着给人用，
    但别让「密码能猜到」变成「别人能拿它当管理员」。
    """
    if not is_admin_enabled():
        return False
    return current_admin(request) is not None


def locked_by_weak_token() -> bool:
    """口令太弱：访客能做的事一律收回，只保留跳转。"""
    return is_admin_enabled() and token_is_weak()


def session_tier(request: Request | None) -> str | None:
    """当前会话的权限层级；未登录返回 None。"""
    if not request or not is_admin_enabled():
        return None
    from app.core.admin import current_tier

    return current_tier(request)


def can_create(db=None, request: Request | None = None) -> bool:
    """访客是否可以创建短链接。"""
    if locked_by_weak_token():
        return session_tier(request) == TIER_ADMIN
    if not is_admin_mode(db):
        return True
    return session_tier(request) == TIER_ADMIN


def can_browse(db=None, request: Request | None = None) -> bool:
    """是否可以查看链接库 / 详情 / 统计 / 导出。

    注意：开放模式也**不**放开这一项。链接库是一份完整的清单，
    公开出去等于把所有人建的短链都摊在阳光下。

    令牌级会话（TIER_OWNER）同样不给：它的存在意义就是「口令泄露后
    还能进去改口令」，要是它也能读库，泄露方照样拿走全部数据。
    """
    return session_tier(request) == TIER_ADMIN


def is_owner(request: Request | None) -> bool:
    """令牌级会话：可以改配置（换口令、删密钥），但看不到任何数据。"""
    return session_tier(request) == TIER_OWNER


def can_manage_settings(request: Request | None) -> bool:
    return session_tier(request) in (TIER_ADMIN, TIER_OWNER)


def may_see(link, request: Request | None, db=None) -> bool:
    """单条链接的可见性：管理员，或持有该链接一次性领取码的创建者。"""
    if session_tier(request) == TIER_ADMIN:
        return True
    if request is None or link is None:
        return False
    from app.core.db import session_scope

    # `?t=` 优先，其次 cookie：二维码图片、换新后的跳转都带不了 query，
    # 但浏览器一定带 cookie。两者都只是「出示这张回执」，安全性同级。
    from app.core.middleware import CLAIM_COOKIE

    token = request.query_params.get("t") or request.cookies.get(CLAIM_COOKIE) or ""
    if not token:
        return False
    from app.core.claim import verify

    with session_scope() as session:
        return verify(session, link, token)


def warnings() -> list[dict]:
    """部署自检：把「看着配了、其实没生效」的地方说清楚。

    这行信息在后台首页挂着比写进 README 有用——README 没人反复读。
    """
    out: list[dict] = []
    if not is_admin_enabled():
        out.append(
            {
                "level": "info",
                "text": "未配置 ADMIN_TOKEN，管理后台处于关闭状态，站点对所有访客开放创建。",
            }
        )
        return out
    if token_is_weak():
        out.append(
            {
                "level": "warn",
                "text": f"ADMIN_TOKEN 短于 {WEAK_TOKEN_LENGTH} 位：访客创建已关闭，访问模式按「仅管理员」执行。"
                "建议用 openssl rand -hex 24 重新生成后再上生产。",
            }
        )
    if settings.access_token and len(settings.access_token) < 8:
        out.append({"level": "warn", "text": "ACCESS_TOKEN 偏短，建议至少 8 位随机串。"})
    return out


def snapshot(db, request: Request | None = None) -> dict:
    mode = current_mode(db)
    admin = bool(request) and is_admin(request, db)
    return {
        "mode": mode,
        "mode_label": LABELS.get(mode, mode),
        "admin_mode": mode == ADMIN,
        "locked": locked_by_weak_token(),
        "warnings": warnings(),
        "is_admin": admin,
        "tier": session_tier(request),
        "can_create": can_create(db, request),
        "can_browse": can_browse(db, request),
        "admin_login_enabled": is_admin_enabled(),
    }
