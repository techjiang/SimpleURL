"""页面路由：跳转、首页、统计页、中转页。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core import auth, guard, runtime
from app.core.config import settings
from app.core.db import get_db
from app.core.security import client_ip
from app.services import (
    LinkUnavailable,
    ServiceError,
    get_link_by_code,
    get_stats,
    log_guard_event,
    record_click,
    resolve_redirect,
    verify_pass,
)
from app.web import render, render_stats

logger = logging.getLogger("simpleurl")
router = APIRouter(tags=["pages"])


@router.get("/healthz", summary="健康检查")
def healthz():
    return {"status": "ok", "site": settings.site_name}


@router.get("/robots.txt", summary="爬虫协议")
def robots():
    body = "User-agent: *\nDisallow: /api/\nDisallow: /admin\n"
    return HTMLResponse(content=body, media_type="text/plain; charset=utf-8")


@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    """根路径的 favicon：浏览器默认会来这儿要，省掉一次 404。"""
    path = settings.static_dir / "favicon.ico"
    if not path.exists():
        raise ServiceError("未找到图标", 404)
    return Response(content=path.read_bytes(), media_type="image/x-icon",
                    headers={"Cache-Control": "public, max-age=604800"})


@router.get("/{code}", summary="短链接跳转")
def redirect(code: str, request: Request, db: Session = Depends(get_db)):
    if len(code) > settings.code_max_length or "." in code:
        raise ServiceError("页面不存在", 404)
    try:
        link, target = resolve_redirect(db, code)
    except LinkUnavailable as exc:
        # 未收录 / 已停用 / 已过期 / 点满：给一个说人话的页面，而不是裸 JSON
        return HTMLResponse(
            render(request, "gone.html", code=code, message=exc.message, reason=exc.reason),
            status_code=exc.status,
        )
    except ServiceError as exc:
        return HTMLResponse(
            render(request, "gone.html", code=code, message=exc.message, reason="missing"),
            status_code=exc.status if exc.status in (404, 410) else 404,
        )

    if link.is_locked:
        # 带口令的短码：先过验证页，跳转与计数都发生在验证通过之后。
        # 口令正确时解锁接口会再 303 回来一次，这次带着 _su_ok 标记，直接放行——
        # 不在浏览器里留长期通行证，一条链接一次一验。
        if request.query_params.get("_su_ok") != "1":
            return HTMLResponse(render(request, "unlock.html", code=link.code, link=link))

    ua = request.headers.get("user-agent")
    ip = client_ip(request)
    verdict = guard.check(link.code, ip, ua)

    if verdict.blocked:
        log_guard_event(db, link.code, verdict.reason, ua, ip, verdict.detail)
        db.commit()
        logger.warning("拦截 %s 的访问：%s（%s）", link.code, verdict.reason, verdict.detail)
        ctx, status = _blocked_context(verdict, target)
        return HTMLResponse(
            render(request, "blocked.html", code=link.code, **ctx),
            status_code=status,
            headers={"Retry-After": str(ctx["retry_after"])},
        )

    if verdict.count_click:
        record_click(db, link, referrer=request.headers.get("referer"), ua=ua, ip=ip)
        db.commit()

    if verdict.interstitial:
        if verdict.reason == "ua":
            log_guard_event(db, link.code, "ua", ua, ip, verdict.detail)
            db.commit()
        # 微信 / QQ 内置浏览器里直接 302 常常打不开，先给一张带「在浏览器打开」的中转页
        return HTMLResponse(
            render(
                request,
                "interstitial.html",
                code=link.code,
                target=target,
                link=link,
                channel=verdict.interstitial,
            )
        )

    logger.info("跳转 %s -> %s", code, target)
    return RedirectResponse(url=target, status_code=307)


def _blocked_context(verdict, target: str) -> tuple[dict, int]:
    """把风控结论翻译成人话，别把内部代号甩给访客。"""
    retry_after = runtime.get_int("guard_block_seconds")
    if verdict.reason == "wechat":
        return (
            {
                "label": "微信内打开",
                "heading": "请复制到浏览器",
                "detail": "微信内置浏览器会拦截部分页面，复制链接到系统浏览器打开即可。",
                "target": target,
                "retry_after": retry_after,
            },
            403,
        )
    if verdict.reason == "qq":
        return (
            {
                "label": "QQ 内打开",
                "heading": "请复制到浏览器",
                "detail": "QQ 内置浏览器会拦截部分页面，复制链接到系统浏览器打开即可。",
                "target": target,
                "retry_after": retry_after,
            },
            403,
        )
    return (
        {
            "label": "访问限制",
            "heading": "先歇一会儿",
            "detail": f"来自你所在网络的访问过于频繁（{verdict.detail}），已临时限制。",
            "target": "",
            "retry_after": retry_after,
        },
        429,
    )


@router.post("/{code}/unlock", response_class=HTMLResponse, summary="口令验证")
async def unlock(code: str, request: Request, db: Session = Depends(get_db)):
    """校验访问口令。成功后直接 303 跳到短码本体，由跳转流程统一计数与风控。

    刻意不在这一步写 Cookie：一次性口令该一次一验，不该在浏览器里留下通行证。
    """
    link = get_link_by_code(db, code)
    form = await request.form()
    provided = form.get("password") or ""
    if not auth.login_allowed(request, limit=20, window=300):
        raise ServiceError("尝试过于频繁，请稍后再试", 429)
    if not verify_pass(link, str(provided)):
        return HTMLResponse(
            render(request, "unlock.html", code=link.code, link=link, error="口令不正确"),
            status_code=403,
        )
    return RedirectResponse(url=f"/{link.code}?_su_ok=1", status_code=303)


@router.get("/s/{code}", response_class=HTMLResponse, summary="统计页")
def stats_page(code: str, request: Request, db: Session = Depends(get_db)):
    if not auth.resolve_access(request).is_admin:
        raise auth.AuthError("访问统计仅管理员可见", 401)
    link = get_link_by_code(db, code)
    data = get_stats(db, code, days=30)
    return HTMLResponse(render_stats(request, link, data))
