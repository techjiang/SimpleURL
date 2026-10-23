"""站点访问模式：区分「开放外部可使用」与「仅管理员可使用」。

- open  ：默认。访客可创建、可查看链接库，管理操作（改/删/后台）仍需管理员
- admin ：访客只能跳转与看统计页，创建、链接库、二维码一律要求管理员会话

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
    """访客是否可以查看链接库 / 详情 / 二维码。"""
    if not is_admin_mode(db):
        return True
    return bool(request) and is_admin(request, db)


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
