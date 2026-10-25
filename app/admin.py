"""管理后台：页面 + 后台专属接口。

页面走 HTML（`/admin`），接口走 JSON（`/api/admin/*`）。鉴权统一复用
`auth.resolve_access` 的签名 Cookie，没有第二套认证，也没有「前端藏按钮」式的
假保护——每个接口都自己校验管理员身份。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import admin_service
from app.core import auth, crypto, guard, runtime
from app.core.clickbuffer import click_buffer
from app.core.config import settings
from app.core.db import get_db
from sqlalchemy import select, update

from app.models import Link
from app.services import ServiceError, delete_link, list_links, update_link
from app.web import render

logger = logging.getLogger("simpleurl")
router = APIRouter(tags=["admin"])

PANELS = ("dashboard", "links", "guard", "keys", "migrate", "settings")


def is_admin(request: Request) -> bool:
    return auth.resolve_access(request).is_admin


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
@router.get("/admin/{panel}", response_class=HTMLResponse, include_in_schema=False)
def admin_page(
    panel: str = "dashboard",
    request: Request = None,
    q: str | None = Query(None),
    status: str | None = Query(None, pattern="^(enabled|disabled|expired)$"),
    sort: str = Query("created_desc", pattern="^(created_desc|created_asc|clicks_desc|clicks_asc|code_asc)$"),
    page: int = Query(1, ge=1),
    days: int = Query(14, ge=7, le=90),
    db: Session = Depends(get_db),
):
    """后台主页。未登录就回登录页，不泄露后台结构。"""
    if panel == "login":
        # /admin/{panel} 会先于主应用的 /admin/login 匹配，这里显式让位
        if is_admin(request):
            return RedirectResponse(url="/admin", status_code=302)
        return HTMLResponse(
            render(request, "login.html", message="请输入管理密钥",
                   access=auth.resolve_access(request))
        )
    if panel not in PANELS:
        panel = "dashboard"
    if not is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=302)
    if not auth.admin_enabled():
        raise auth.AuthError("服务端未配置 ADMIN_TOKEN，管理功能未启用", 403)

    context = {
        "panel": panel,
        "panels": [
            ("dashboard", "仪表盘", "◎"),
            ("links", "链接管理", "≡"),
            ("guard", "防洪 / UA", "⛨"),
            ("keys", "API 密钥", "⚿"),
            ("migrate", "数据迁移", "⇄"),
            ("settings", "设置", "⚙"),
        ],
        "settings_view": runtime.describe(),
        "access": auth.resolve_access(request),
    }
    if panel == "dashboard":
        context["dash"] = admin_service.dashboard(db, days=days)
        context["events"] = admin_service.recent_guard_events(db)
        context["buffer_stats"] = click_buffer.stats().__dict__
        context["guard_env"] = guard.env_snapshot()
    elif panel == "links":
        total, items = list_links(db, q=q, status=status, sort=sort, page=page, page_size=20)
        pages = max(1, -(-total // 20))
        context.update(
            {
                "total": total,
                "links": [row.to_dict() for row in items],
                "page": min(page, pages),  # 手写 ?page=99 时别显示「第 99/1 页」这种自相矛盾的文案
                "pages": pages,
                "q": q or "",
                "status": status or "",
                "sort": sort,
            }
        )
    elif panel == "guard":
        context["guard_env"] = guard.env_snapshot()
        context["events"] = admin_service.recent_guard_events(db, limit=50)
        context["buffer_stats"] = click_buffer.stats().__dict__
    elif panel == "keys":
        context["keys"] = admin_service.list_api_keys(db)
    return HTMLResponse(render(request, "admin.html", **context))


# ---------------- 接口 ----------------
admin_api = APIRouter(prefix="/api/admin", tags=["admin"])


def require(request: Request) -> auth.Access:
    access = auth.resolve_access(request)
    if not access.is_admin:
        raise auth.AuthError("仅管理员可访问", 401)
    return access


@admin_api.get("/overview", summary="后台仪表盘数据")
def overview(request: Request, days: int = Query(14, ge=7, le=90), db: Session = Depends(get_db)):
    require(request)
    data = admin_service.dashboard(db, days=days)
    data["queue"] = click_buffer.stats().__dict__
    data["guard"] = guard.env_snapshot()
    data["events"] = admin_service.recent_guard_events(db, limit=10)
    return data


@admin_api.get("/settings", summary="读取运行配置")
def get_settings_api(request: Request):
    require(request)
    return {"items": runtime.describe(), "values": runtime.snapshot()}


@admin_api.post("/settings", summary="保存运行配置")
async def save_settings_api(request: Request):
    require(request)
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise ServiceError("请求体不是合法 JSON", 400) from exc
    if not isinstance(payload, dict):
        raise ServiceError("请求体应为对象", 400)
    applied = runtime.set_many(payload)
    return {"ok": True, "applied": applied, "values": runtime.snapshot()}


@admin_api.get("/duplicate", summary="按目标地址查重")
def duplicate_api(request: Request, target: str = Query(..., min_length=1), db: Session = Depends(get_db)):
    """创建前查一下这个目标是否已经收录，避免同一页面存出好几条短码。"""
    require(request)
    from app.services import find_similar

    return {"items": find_similar(db, target)}


@admin_api.get("/keys", summary="API 密钥列表")
def list_keys(request: Request, db: Session = Depends(get_db)):
    require(request)
    return {"items": admin_service.list_api_keys(db)}


@admin_api.post("/keys", summary="创建 API 密钥")
async def create_key(request: Request, db: Session = Depends(get_db)):
    require(request)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    name = str((payload or {}).get("name") or "未命名")[:64]
    raw, record = auth.create_api_key(db, name)
    db.commit()
    # 明文只在这里出现一次，之后库里只有哈希
    return {"ok": True, "key": raw, "item": record.to_dict()}


@admin_api.delete("/keys/{key_id}", summary="删除 API 密钥")
def remove_key(key_id: int, request: Request, db: Session = Depends(get_db)):
    require(request)
    admin_service.delete_api_key(db, key_id)
    db.commit()
    return {"ok": True}


@admin_api.post("/migrate", summary="导入 YOURLS 数据")
async def migrate(request: Request, db: Session = Depends(get_db)):
    require(request)
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise ServiceError("请求体不是合法 JSON", 400) from exc
    raw = str((payload or {}).get("content") or "")
    conflict = str((payload or {}).get("conflict") or "rename")
    return admin_service.import_yourls(db, raw, conflict=conflict)


@admin_api.get("/export", summary="导出全部链接（JSON）")
def export_all(request: Request, db: Session = Depends(get_db)):
    require(request)
    return {"items": admin_service.export_links(db)}


@admin_api.post("/purge", summary="清理历史点击明细")
async def purge(request: Request, db: Session = Depends(get_db)):
    require(request)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    days = int((payload or {}).get("before_days") or 180)
    removed = admin_service.purge_clicks(db, before_days=days)
    db.commit()
    return {"ok": True, "removed": removed}


@admin_api.post("/flush", summary="立即结算点击缓冲")
def flush_clicks(request: Request):
    require(request)
    return {"ok": True, "written": click_buffer.flush()}


@admin_api.post("/links/{code}/toggle", summary="启用 / 停用链接")
def toggle_link(code: str, request: Request, db: Session = Depends(get_db)):
    require(request)
    from app.schemas import LinkUpdate
    from app.services import get_link_by_code

    link = get_link_by_code(db, code)
    update_link(db, code, LinkUpdate(enabled=not link.enabled))
    db.commit()
    return {"ok": True, "enabled": not link.enabled}


@admin_api.delete("/links/{code}", summary="删除链接")
def admin_delete_link(code: str, request: Request, db: Session = Depends(get_db)):
    require(request)
    delete_link(db, code)
    db.commit()
    return {"ok": True}


@admin_api.get("/whoami", summary="当前身份")
def whoami(request: Request):
    access = auth.resolve_access(request)
    return {
        "is_admin": access.is_admin,
        "mode": access.mode,
        "fingerprint": crypto.fingerprint(settings.admin_token) if settings.admin_token else "",
    }


@admin_api.post("/danger/clear-clicks", summary="清零统计（保留链接）")
def danger_clear_clicks(request: Request, db: Session = Depends(get_db)):
    """把点击明细与累计数一起清零，链接本身保留。误点不可撤销。"""
    require(request)
    from app.models import ClickEvent, GuardEvent

    for row in db.execute(select(ClickEvent)).scalars().all():
        db.delete(row)
    for row in db.execute(select(GuardEvent)).scalars().all():
        db.delete(row)
    db.execute(update(Link).values(clicks=0))
    db.commit()
    return {"ok": True}


@admin_api.post("/danger/clear-all", summary="清空全部链接")
def danger_clear_all(request: Request, db: Session = Depends(get_db)):
    require(request)
    from app.models import ClickEvent, GuardEvent

    for row in db.execute(select(ClickEvent)).scalars().all():
        db.delete(row)
    for row in db.execute(select(GuardEvent)).scalars().all():
        db.delete(row)
    for row in db.execute(select(Link)).scalars().all():
        db.delete(row)
    db.commit()
    return {"ok": True}


def json_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "message": message})
