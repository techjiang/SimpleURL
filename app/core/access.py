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
from app.core.settings_store import effective

OPEN = "open"
ADMIN = "admin"
MODES = (OPEN, ADMIN)
LABELS = {OPEN: "开放外部可使用", ADMIN: "仅管理员可使用"}


def normalize_mode(raw: object) -> str:
    mode = str(raw or "").strip().lower()
    return mode if mode in MODES else OPEN


def current_mode(db=None) -> str:
    """当前生效模式。没配管理令牌时永远开放，防止配错锁死站点。"""
    if not is_admin_enabled():
        return OPEN
    return normalize_mode(effective(db, "access_mode"))


def is_admin_mode(db=None) -> bool:
    return current_mode(db) == ADMIN


def is_admin(request: Request, db=None) -> bool:
    """当前请求是否已通过管理员登录。"""
    if not is_admin_enabled():
        return False
    return current_admin(request) is not None


def can_create(db=None, request: Request | None = None) -> bool:
    """访客是否可以创建短链接。"""
    if not is_admin_mode(db):
        return True
    return bool(request) and is_admin(request, db)


def can_browse(db=None, request: Request | None = None) -> bool:
    """是否可以查看链接库 / 详情 / 统计 / 导出。

    注意：开放模式也**不**放开这一项。链接库是一份完整的清单，
    公开出去等于把所有人建的短链都摊在阳光下。
    """
    return bool(request) and is_admin(request, db)


def may_see(link, request: Request | None, db=None) -> bool:
    """单条链接的可见性：管理员，或持有该链接一次性领取码的创建者。"""
    if is_admin(request, db):
        return True
    if request is None or link is None:
        return False
    from app.core.db import session_scope

    token = request.query_params.get("t") or ""
    if not token:
        return False
    from app.core.claim import verify

    with session_scope() as session:
        return verify(session, link, token)


def snapshot(db, request: Request | None = None) -> dict:
    mode = current_mode(db)
    admin = bool(request) and is_admin(request, db)
    return {
        "mode": mode,
        "mode_label": LABELS.get(mode, mode),
        "admin_mode": mode == ADMIN,
        "is_admin": admin,
        "can_create": can_create(db, request),
        "can_browse": can_browse(db, request),
        "admin_login_enabled": is_admin_enabled(),
    }
