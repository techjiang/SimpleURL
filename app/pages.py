"""页面路由：跳转、首页、统计页。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import client_ip
from app.services import ServiceError, get_link_by_code, get_stats, record_click, resolve_redirect
from app.web import render_stats, templates

logger = logging.getLogger("simpleurl")
router = APIRouter(tags=["pages"])


@router.get("/healthz", summary="健康检查")
def healthz():
    return {"status": "ok", "site": settings.site_name}


@router.get("/robots.txt", summary="爬虫协议")
def robots():
    body = "User-agent: *\nDisallow: /api/\nDisallow: /admin\n"
    return HTMLResponse(content=body, media_type="text/plain; charset=utf-8")


@router.get("/{code}", summary="短链接跳转")
def redirect(code: str, request: Request, db: Session = Depends(get_db)):
    if len(code) > settings.code_max_length or "." in code:
        raise ServiceError("页面不存在", 404)
    try:
        link, target = resolve_redirect(db, code)
    except ServiceError as exc:
        # 未收录 / 已停用 / 已过期：给一个友好页面，而不是裸 JSON
        return HTMLResponse(
            templates.get_template("gone.html").render(
                request=request, code=code, message=exc.message
            ),
            status_code=exc.status if exc.status in (404, 410) else 404,
        )
    referrer = request.headers.get("referer") or request.headers.get("referrer")
    ua = request.headers.get("user-agent")
    record_click(db, link, referrer=referrer, ua=ua, ip=client_ip(request))
    db.commit()
    logger.info("跳转 %s -> %s", code, target)
    return RedirectResponse(url=target, status_code=307)


@router.get("/s/{code}", response_class=HTMLResponse, summary="统计页")
def stats_page(code: str, request: Request, db: Session = Depends(get_db)):
    link = get_link_by_code(db, code)
    data = get_stats(db, code, days=30)
    return HTMLResponse(render_stats(request, link, data))
