"""页面路由：跳转、首页、统计页。"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import __version__
from app.core import access, guard, policy, social
from app.core.admin import current_admin
from app.core.config import settings
from app.core.db import get_db
from app.core.middleware import ACCESS_COOKIE
from app.core.security import client_ip
from app.services import ServiceError, get_link_by_code, get_stats, record_click, resolve_redirect
from app.web import branding, render, render_stats

logger = logging.getLogger("simpleurl")
router = APIRouter(tags=["pages"])

# 页面路径，不能被短码占用
# 站内真实存在的路径，一个都不能被短码占用。这份清单是唯一来源：
# 建链时的校验（core.codes.reserved_conflict）也读它。
# 以前两边各写一份，`tools` 和 `yourls-api` 只在这里出现，结果能建出来，
# 路由注册顺序又让它们永远到不了 —— 建的时候不报错，用起来跳去别处。
RESERVED_PATHS = {
    "healthz",
    "robots.txt",
    "tools",
    "s",
    "static",
    "api",
    "admin",
    "favicon.ico",
    "manifest.webmanifest",
    "yourls-api",
    "apple-touch-icon.png",
    "apple-touch-icon-precomposed.png",
}


def _check_access(request: Request) -> bool:
    """配置了 ACCESS_TOKEN 时，校验查询参数或 Cookie 里的令牌。

    前端门禁存的是 sessionStorage 里的明文，靠拼 `?token=` 跳转；
    这里必须真的认那个令牌，否则「输对了令牌还是 403」——门禁形同摆设。
    """
    if not settings.access_token:
        return True
    if getattr(request.state, "access_token_ok", False):
        return True
    token = request.query_params.get("token") or request.cookies.get(ACCESS_COOKIE)
    return hmac.compare_digest(token or "", settings.access_token)


def _access_denied(request: Request, code: str) -> HTMLResponse:
    return HTMLResponse(
        render(
            "gone.html",
            request,
            None,
            code=code,
            message="访问受限：请携带正确的访问令牌",
            detail="这个站点开了访问令牌保护，链接里带上 ?token=你的令牌 就能打开。",
        ),
        status_code=403,
    )


@router.get("/admin", response_class=HTMLResponse, summary="管理后台")
def admin_page(request: Request):
    """后台页本身不卡登录，前端按 /api/admin/session 结果自行弹登录框。"""
    return HTMLResponse(render("admin.html", request, None))


def _owner_claim(request: Request, db: Session, link) -> str | None:
    """这条链接的创建者手里的回执明文（没有就 None）。

    「链接失效了」这件事对创建者和其他人意义完全不同：别人只是点不开，
    创建者可以当场修。所以友好页要两副面孔。
    """
    from app.core.middleware import CLAIM_COOKIE

    token = request.query_params.get("t") or request.cookies.get(CLAIM_COOKIE) or ""
    if not token or link is None:
        return None
    from app.core.claim import verify

    return token if verify(db, link, token) else None


def _gone_page(request: Request, code: str, message: str, detail: str, status_code: int = 404) -> HTMLResponse:
    """统一渲染「链接用不了」页：不裸 JSON，也不给一个像故障的白屏。"""
    return HTMLResponse(
        render("gone.html", request, None, code=code, message=message, detail=detail),
        status_code=status_code,
    )


def _link_gone(request: Request, db: Session, code: str, message: str, detail: str, status_code: int = 404):
    """链接用不了时给的那一页。

    创建者（手里有回执）与普通访客看到的不是同一页：前者能当场续期、
    看到自己那条剩下的账，后者只需要一句「为什么打不开」和一个能走的路。
    """
    from app.services import ServiceError

    try:
        link = get_link_by_code(db, code)
    except ServiceError:
        # 「查无此码」是最常见的一种，不该因此退回裸 JSON
        link = None
    token = _owner_claim(request, db, link) if link is not None else None
    if token is None:
        return _gone_page(request, code, message, detail, status_code=status_code)
    state = "已过期" if link.is_expired else ("已停用" if not link.enabled else "正常")
    return HTMLResponse(
        render(
            "gone.html",
            request,
            db,
            code=code,
            message=message,
            detail=detail,
            owner=True,
            claim_token=token,
            state=state,
            clicks=link.clicks,
            expires_at=link.expires_at,
            target=link.target,
            action_url=f"/s/{code}?t={token}",
            action_label="查看这条的统计",
        ),
        status_code=status_code,
    )


def _social_blocked(snapshot: dict, platform: str | None) -> str | None:
    """命中拦截策略时返回平台键。总开关关掉就一律放行。"""
    if not platform or not snapshot.get("social_guard"):
        return None
    return guard.platform_hit(platform, snapshot.get("platforms") or {})


@router.api_route("/healthz", methods=["GET", "HEAD"], summary="健康检查")
def healthz(db: Session = Depends(get_db)):
    """给探针和运维看的健康检查。

    状态码仍然只看「进程活着 + 库能读」；排队、丢数据这些细节放在 body 里，
    别让一个待结算队列把 k8s 的 liveness 探针打成重启循环。
    """
    from app.core.clicks import click_buffer
    from app.services import runtime_status

    try:
        status = runtime_status(db)
    except Exception as exc:  # noqa: BLE001 - 库读不了才是真的不健康
        logger.exception("健康检查读库失败")
        return JSONResponse({"status": "degraded", "site": settings.site_name, "error": str(exc)[:200]}, 503)


    state = "ok"
    if status["pending_clicks"] > settings.click_buffer_max * 0.8:
        state = "degraded"
    return {
        "status": state,
        "site": branding(db)["site_name"],
        "version": __version__,
        "pending_clicks": status["pending_clicks"],
        "dropped_clicks": status["dropped_clicks"],
        "db_size": status["db_size"],
    }


@router.api_route("/robots.txt", methods=["GET", "HEAD"], summary="爬虫协议")
def robots():
    # 统计页与后台一样是私人数据，别让爬虫把「谁点了多少」收进索引
    body = "User-agent: *\nDisallow: /api/\nDisallow: /admin\nDisallow: /s/\n"
    return HTMLResponse(content=body, media_type="text/plain; charset=utf-8")


# 顺序要紧：这个路由必须写在 `/{code}` 之前。通配路由谁先注册谁先吃请求，
# 写后面的话 `/tools` 会被当成一条短码，直接回「页面不存在」。
@router.get("/tools", response_class=HTMLResponse, summary="接口与工具")
def tools_page(request: Request, db: Session = Depends(get_db)):
    """接口文档 + 可以直接点一次的试运行。

    API 一直只有 README 里几行 curl，真要接进别人的系统时还是得翻源码。
    文档放在站内还有个好处：Base URL 自动跟着部署域名走，不会写死。
    """
    return HTMLResponse(render("tools.html", request, db, endpoints=ENDPOINTS))


@router.get("/{code}", summary="短链接跳转")
@router.head("/{code}", include_in_schema=False)
def redirect(code: str, request: Request, db: Session = Depends(get_db)):
    # 含 . 的按资源路径处理（sitemap.xml、ads.txt 之类），403 / 404 一律回 404，
    # 别再把人 302 到首页——那看着像「链接坏了但系统还在笑」
    if "." in code:
        return _link_gone(request, db, code, "页面不存在", "这里没有你要找的东西。", status_code=404)
    if len(code) > settings.code_max_length or code.lower() in RESERVED_PATHS:
        # 以前这里抛 ServiceError，落到全局处理器上就是一句裸 JSON，
        # 和「未收录的短码给友好页」两套待遇。保留字只是「这个名字不给用」，路人也该看到同一页。
        return _link_gone(request, db, code, "页面不存在", "这里没有你要找的东西。", status_code=404)
    if not _check_access(request):
        return _access_denied(request, code)
    try:
        link, target = resolve_redirect(db, code)
    except ServiceError as exc:
        # 未收录 / 已停用 / 已过期：给一个友好页面，而不是裸 JSON。
        # 如果来的是创建者本人，页面会多一个「续期并重新启用」的按钮。
        return _link_gone(
            request,
            db,
            code,
            exc.message,
            "这个短链接已经不能用了。它可能被删除、被停用，或者到了过期时间。",
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
            render(
                "wxblock.html",
                request,
                db,
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
            render(
                "gone.html",
                request,
                db,
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


# 接口清单按「做什么」分组，不按 HTTP 方法。文档是写给人看的，
# 人要找的永远是「我要建一条链接」而不是「POST 有哪些」。
ENDPOINTS: dict[str, list[dict]] = {
    "短链接": [
        {"method": "POST", "path": "/api/links", "desc": "创建短链接，可用回执回看", "auth": ""},
        {"method": "GET", "path": "/api/links", "desc": "链接库分页、搜索、筛选、排序", "auth": "管理员"},
        {"method": "GET", "path": "/api/links/{code}", "desc": "单条详情", "auth": "管理员"},
        {"method": "PATCH", "path": "/api/links/{code}", "desc": "改目标 / 标题 / 标签 / 过期时间", "auth": "管理员"},
        {"method": "DELETE", "path": "/api/links/{code}", "desc": "删除", "auth": "管理员"},
        {"method": "POST", "path": "/api/links/bulk", "desc": "批量启用 / 停用 / 删除", "auth": "管理员"},
        {"method": "GET", "path": "/api/links/export", "desc": "按当前筛选导出 CSV", "auth": "管理员"},
    ],
    "统计与资源": [
        {"method": "GET", "path": "/api/links/{code}/stats", "desc": "点击趋势、来源、设备、平台", "auth": "管理员 / 回执"},
        {"method": "GET", "path": "/api/qr/{code}", "desc": "二维码 PNG", "auth": "管理员 / 回执"},
        {"method": "GET", "path": "/api/stats/summary", "desc": "站点总览", "auth": ""},
        {"method": "GET", "path": "/api/tags", "desc": "标签及出现次数", "auth": ""},
        {"method": "GET", "path": "/api/access", "desc": "当前访问模式与站点信息", "auth": ""},
    ],
    "密钥与运维": [
        {"method": "GET", "path": "/api/admin/session", "desc": "当前登录态", "auth": ""},
        {"method": "POST", "path": "/api/admin/login", "desc": "后台登录", "auth": ""},
        {"method": "GET", "path": "/api/admin/dashboard", "desc": "仪表盘数据", "auth": "管理员"},
        {"method": "GET", "path": "/api/admin/runtime", "desc": "运行状态与部署自检", "auth": "管理员"},
        {"method": "GET", "path": "/api/admin/audit", "desc": "操作留痕", "auth": "管理员"},
        {"method": "GET", "path": "/healthz", "desc": "健康检查，含待结算与丢弃计数", "auth": ""},
    ],
}


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
            render(
                "gone.html",
                request,
                db,
                code=code,
                message=message,
                detail=detail,
                action_url="/admin",
                action_label="去后台登录",
            ),
            status_code=401,
        )
    data = get_stats(db, code, days=30)
    return HTMLResponse(render_stats(request, link, data, db))
