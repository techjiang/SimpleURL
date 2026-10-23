"""REST API 路由。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import auth
from app.core.auth import Access, get_access, require_admin, require_create
from app.core.config import settings
from app.core.db import get_db
from app.core.security import client_ip
from app.models import ClickEvent, Link
from app.qr import make_qr_png
from app.schemas import LinkCreate, LinkListOut, LinkOut, LinkUpdate, StatsOut
from app.services import (
    create_link,
    delete_link,
    get_link_by_code,
    get_stats,
    list_links,
    update_link,
)

logger = logging.getLogger("simpleurl")
router = APIRouter(prefix="/api", tags=["api"])


def _serialize(link: Link, request: Request) -> LinkOut:
    out = LinkOut.model_validate(link.to_dict())
    out.short_url = str(request.base_url).rstrip("/") + "/" + link.code
    return out


@router.get("/links", response_model=LinkListOut, summary="短链接列表")
def api_list_links(
    request: Request,
    _: Access = Depends(require_admin),
    q: str | None = Query(None, description="搜索短码/目标/标题/标签"),
    tag: str | None = None,
    status: str | None = Query(None, pattern="^(enabled|disabled|expired)$"),
    sort: str = Query("created_desc", pattern="^(created_desc|created_asc|clicks_desc|clicks_asc|code_asc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    total, items = list_links(db, q=q, tag=tag, status=status, sort=sort, page=page, page_size=page_size)
    return LinkListOut(
        total=total, page=page, page_size=page_size, items=[_serialize(item, request) for item in items]
    )


@router.post("/links", response_model=LinkOut, status_code=201, summary="创建短链接")
def api_create_link(
    payload: LinkCreate,
    request: Request,
    access: Access = Depends(require_create),
    db: Session = Depends(get_db),
):
    link = create_link(db, payload, ip=client_ip(request))
    db.commit()
    db.refresh(link)
    return _serialize(link, request)


@router.get("/links/{code}", response_model=LinkOut, summary="短链接详情")
def api_get_link(code: str, request: Request, _: Access = Depends(require_admin), db: Session = Depends(get_db)):
    return _serialize(get_link_by_code(db, code), request)


@router.patch("/links/{code}", response_model=LinkOut, summary="更新短链接")
def api_update_link(
    code: str,
    payload: LinkUpdate,
    request: Request,
    _: Access = Depends(require_admin),
    db: Session = Depends(get_db),
):
    link = update_link(db, code, payload)
    db.commit()
    db.refresh(link)
    return _serialize(link, request)


@router.delete("/links/{code}", summary="删除短链接")
def api_delete_link(code: str, _: Access = Depends(require_admin), db: Session = Depends(get_db)):
    delete_link(db, code)
    db.commit()
    return {"ok": True, "message": f"已删除 {code}"}


@router.get("/links/{code}/stats", response_model=StatsOut, summary="访问统计")
def api_link_stats(
    code: str,
    days: int = Query(30, ge=1, le=365),
    _: Access = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return StatsOut(**get_stats(db, code, days=days))


@router.get("/qr/{code}", summary="二维码 PNG")
def api_qr(
    code: str,
    request: Request,
    secret: str | None = Header(None, alias="X-Admin-Token"),
    db: Session = Depends(get_db),
):
    link = get_link_by_code(db, code)
    if settings.effective_mode in ("admin", "invite"):
        # 私有模式下二维码里的短链必须能被访问，未登录则拒绝，避免泄露目标
        if not auth.resolve_access(request).is_admin and not auth.admin_password_ok(secret):
            raise auth.AuthError("仅管理员可获取二维码", 401)
    url = str(request.base_url).rstrip("/") + "/" + link.code
    return Response(
        content=make_qr_png(url),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/stats/summary", summary="总览统计")
def api_summary(access: Access = Depends(get_access), db: Session = Depends(get_db)):
    if access.is_admin:
        total_links = db.execute(select(func.count()).select_from(Link)).scalar_one()
        total_clicks = db.execute(select(func.count()).select_from(ClickEvent)).scalar_one()
    else:
        total_links = total_clicks = 0
    return {
        "total_links": int(total_links or 0),
        "total_clicks": int(total_clicks or 0),
        "site_name": settings.site_name,
        "mode": access.mode,
        "is_admin": access.is_admin,
    }


# ---------------- 访问模式 / 登录 ----------------
_SESSION_MAX_AGE = auth.COOKIE_MAX_AGE


def _is_https(request: Request) -> bool:
    """判断本次请求是否走 https。

    直连时看 scheme；挂在反代后面时看 X-Forwarded-Proto。
    """
    if request.url.scheme == "https":
        return True
    proto = request.headers.get("x-forwarded-proto", "")
    return proto.split(",")[0].strip().lower() == "https"


def _set_session(request: Request, response: Response, name: str, value: str) -> None:
    response.set_cookie(
        name,
        value,
        max_age=_SESSION_MAX_AGE,
        httponly=True,   # JS 读不到，挡掉 XSS 偷 Cookie
        samesite="lax",  # 站外 POST 不带 Cookie，挡住跨站伪造请求
        secure=_is_https(request),  # https 下才加 Secure，防明文嗅探
        path="/",
    )


@router.get("/access", summary="当前访问模式与身份")
def api_access(access: Access = Depends(get_access)):
    return access.as_dict()


@router.post("/admin/login", summary="管理员登录")
def api_login(
    request: Request,
    response: Response,
    secret: str | None = Header(None, alias="X-Admin-Token"),
    access: Access = Depends(get_access),
):
    # 密钥只从请求头取。URL query 会进访问日志、浏览器历史和 Referer，
    # 那等于把钥匙抄在门上，所以这里明确不接受。
    if not secret:
        raise auth.AuthError("请通过 X-Admin-Token 请求头提供管理密钥", 400)
    if access.is_admin:
        return {"ok": True, "is_admin": True}
    if not auth.admin_enabled():
        raise auth.AuthError("服务端未配置管理密钥，无法登录", 400)
    if not auth.login_allowed(request):
        raise auth.AuthError("尝试过于频繁，请稍后再试", 429)
    if not auth.admin_password_ok(secret):
        # 不区分「密钥错」和「没配密钥」，避免探测站点状态
        logger.warning("管理密钥校验失败，来源 IP %s", client_ip(request) or "unknown")
        raise auth.AuthError("管理密钥不正确", 401)
    _set_session(request, response, auth.ADMIN_COOKIE, auth.admin_session())
    return {"ok": True, "is_admin": True}


@router.post("/admin/logout", summary="退出登录")
def api_logout(response: Response):
    response.delete_cookie(auth.ADMIN_COOKIE)
    response.delete_cookie(auth.INVITE_COOKIE)
    return {"ok": True, "is_admin": False}


@router.get("/admin/invite", summary="查看当前邀请码（管理员）")
def api_invite(_: Access = Depends(require_admin)):
    return {"invite_code": auth.invite_key(), "expires_in": auth.COOKIE_MAX_AGE}


@router.post("/invite", summary="用邀请码解锁创建")
def api_redeem_invite(request: Request, response: Response, code: str = Query(..., min_length=1)):
    if settings.effective_mode != auth.MODE_INVITE:
        raise auth.AuthError("当前站点未启用邀请码模式", 400)
    if not auth.login_allowed(request):
        raise auth.AuthError("尝试过于频繁，请稍后再试", 429)
    if not auth.invite_ok(code):
        raise auth.AuthError("邀请码无效或已过期", 403)
    _set_session(request, response, auth.INVITE_COOKIE, auth.invite_session())
    return {"ok": True, "can_create": True}
