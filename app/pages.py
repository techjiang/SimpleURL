"""页面路由：跳转、首页、统计页。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core import access, guard, policy, social
from app.core.admin import current_admin
from app.core.config import settings
from app.core.db import get_db
from app.core.security import client_ip
from app.services import ServiceError, get_link_by_code, get_stats, record_click, resolve_redirect
from app.web import render_stats, templates

logger = logging.getLogger("simpleurl")
router = APIRouter(tags=["pages"])

# 页面路径，不能被短码占用
RESERVED_PATHS = {
    "healthz",
    "robots.txt",
    "s",
    "static",
    "api",
    "admin",
    "favicon.ico",
    "manifest.webmanifest",
    "yourls-api",
}


def _check_access(request: Request) -> bool:
    """配置了 ACCESS_TOKEN 时，校验查询参数或 Cookie 里的令牌。"""
    if not settings.access_token:
        return True
    token = request.query_params.get("token") or request.cookies.get("access_token")
    return token == settings.access_token


def _access_denied(request: Request, code: str) -> HTMLResponse:
    return HTMLResponse(
        templates.get_template("gone.html").render(
            request=request,
            code=code,
            message="访问受限：请携带正确的访问令牌",
            detail="这个站点开了访问令牌保护，链接里带上 ?token=你的令牌 就能打开。",
        ),
        status_code=403,
    )


@router.get("/admin", response_class=HTMLResponse, summary="管理后台")
def admin_page(request: Request):
    """后台页本身不卡登录，前端按 /api/admin/session 结果自行弹登录框。"""
    return HTMLResponse(templates.get_template("admin.html").render(request=request))


def _social_blocked(snapshot: dict, platform: str | None) -> str | None:
    """命中拦截策略时返回平台键。总开关关掉就一律放行。"""
    if not platform or not snapshot.get("social_guard"):
        return None
    return guard.platform_hit(platform, snapshot.get("platforms") or {})


@router.get("/healthz", summary="健康检查")
def healthz():
    return {"status": "ok", "site": settings.site_name}


@router.get("/robots.txt", summary="爬虫协议")
def robots():
    body = "User-agent: *\nDisallow: /api/\nDisallow: /admin\n"
    return HTMLResponse(content=body, media_type="text/plain; charset=utf-8")


@router.get("/{code}", summary="短链接跳转")
def redirect(code: str, request: Request, db: Session = Depends(get_db)):
    if len(code) > settings.code_max_length or "." in code or code.lower() in RESERVED_PATHS:
        raise ServiceError("页面不存在", 404)
    if not _check_access(request):
        return _access_denied(request, code)
    try:
        link, target = resolve_redirect(db, code)
    except ServiceError as exc:
        # 未收录 / 已停用 / 已过期：给一个友好页面，而不是裸 JSON
        return HTMLResponse(
            templates.get_template("gone.html").render(
                request=request,
                code=code,
                message=exc.message,
                detail="这个短链接已经不能用了。它可能被删除、被停用，或者到了过期时间。",
            ),
            status_code=exc.status if exc.status in (404, 410) else 404,
        )
    referrer = request.headers.get("referer") or request.headers.get("referrer")
    ua = request.headers.get("user-agent")
    ip = client_ip(request)

    # 一次取齐本请求要用的策略（频率上限 / 社交总开关 / 平台策略 / UA 规则），
    # 热门短码上这几次读会被快照合并掉
    snap = policy.snapshot(db)

    # 社交平台防洪：微信 / QQ 等内置浏览器可能被策略拦下，给一页说明而不是白屏
    platform = social.detect(ua, referrer)
    blocked_platform = _social_blocked(snap, platform)
    if blocked_platform:
        logger.info("社交平台拦截 %s -> %s", code, blocked_platform)
        # 拦下的访问也算一次「被拒」，落一条 blocked 事件，后台看得到战果
        record_click(db, link, referrer=referrer, ua=ua, ip=ip, blocked=True, snapshot=snap)
        db.commit()
        return HTMLResponse(
            templates.get_template("wxblock.html").render(
                request=request,
                code=code,
                platform=social.label(blocked_platform),
                notice=guard.platform_notice(db),
                target=target,
            ),
            status_code=403,
        )

    allowed, reason = guard.check_redirect_quota(db, ip, snap)
    if not allowed:
        return HTMLResponse(
            templates.get_template("gone.html").render(
                request=request,
                code=code,
                message=reason,
                detail="这是防洪限速在起作用，不是链接本身出了问题，稍等一会儿再试就好。",
            ),
            status_code=429,
        )

    # 延迟计数：只入缓冲，不阻塞跳转
    record_click(db, link, referrer=referrer, ua=ua, ip=ip, snapshot=snap)
    db.commit()
    logger.info("跳转 %s -> %s", code, target)
    return RedirectResponse(url=target, status_code=307)


@router.get("/s/{code}", response_class=HTMLResponse, summary="统计页")
def stats_page(code: str, request: Request, db: Session = Depends(get_db)):
    if not _check_access(request):
        return _access_denied(request, code)
    link = get_link_by_code(db, code)
    # 统计页只对管理员开放；创建者手里的一次性回执（?t=）放行自己那一条
    if not access.may_see(link, request, db):
        if access.is_admin_mode(db):
            message = "站点已设为仅管理员可使用，请先登录后台"
            detail = "这个站点只允许管理员使用，登录后即可查看统计。"
        else:
            message = "统计页仅管理员可见"
            detail = "创建链接时会给你一条带领取码的回执链接，凭它查看自己那条的统计。"
        return HTMLResponse(
            templates.get_template("gone.html").render(
                request=request,
                code=code,
                message=message,
                detail=detail,
                action_url="/admin",
                action_label="去后台登录",
            ),
            status_code=401,
        )
    data = get_stats(db, code, days=30)
    return HTMLResponse(render_stats(request, link, data))
