"""YOURLS 兼容接口：让已有的 YOURLS 客户端/插件无缝切过来。

YOURLS 客户端历来都是把参数拼在 query string 上，所以这里以 query 为主、
表单 body 为辅。body 解析失败不影响主流程。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import client_ip
from app.models import Link
from app.schemas import LinkCreate
from app.services import ServiceError, apply_title, create_link, fetch_title_for, get_link_by_code, get_stats

logger = logging.getLogger("simpleurl")
router = APIRouter(prefix="/yourls-api", tags=["yourls"])


def install(app) -> None:
    """把表单缓存中间件挂到应用上。"""
    app.add_middleware(_FormCacheMiddleware)

SUPPORTED_ACTIONS = {"shorturl", "expand", "url-stats", "stats", "db-stats"}


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _params(request: Request) -> dict:
    """query 优先，表单 body 补充（中间件读不到就算了，不报错）。"""
    params = dict(request.query_params)
    body = getattr(request.state, "form", None)
    if body:
        for key, value in body.items():
            params.setdefault(key, value)
    return params


class _FormCacheMiddleware(BaseHTTPMiddleware):
    """把 POST 的表单 body 缓存到 request.state，供路由读取。

    Starlette 的 request.form() 只能读一次，且不同 httpx 版本下 TestClient
    发 form 的行为有差异，所以在中间件里统一读一次最稳妥。
    """

    async def dispatch(self, request: Request, call_next):
        if request.method == "POST":
            try:
                request.state.form = await request.form()
            except Exception:
                request.state.form = None
        return await call_next(request)


def _check_signature(params: dict, request: Request) -> None:
    """只有显式配了 YOURLS_TOKEN 才校验签名，不默认继承管理令牌。"""
    if not settings.yourls_token:
        return
    sig = params.get("signature") or request.headers.get("X-YOURLS-Signature")
    if sig != settings.yourls_token:
        raise ServiceError("签名不正确", 403)


def _code_from(shorturl: str) -> str:
    """从短地址里抠出短码，兼容带不带协议、带不带斜杠。"""
    value = (shorturl or "").strip().rstrip("/")
    if not value:
        return ""
    return value.rsplit("/", 1)[-1]


@router.post("", summary="YOURLS 兼容 API")
def yourls_api(request: Request, db: Session = Depends(get_db)):
    params = _params(request)
    _check_signature(params, request)

    action = (params.get("action") or "shorturl").strip()
    if action not in SUPPORTED_ACTIONS:
        raise ServiceError(f"不支持的动作：{action}", 400)

    if action == "shorturl":
        url = (params.get("url") or "").strip()
        if not url:
            return {"status": "fail", "code": "error:nourl", "message": "缺少 url 参数", "errorCode": "400"}
        keyword = (params.get("keyword") or "").strip() or None
        title = (params.get("title") or "").strip() or None
        try:
            link = create_link(db, LinkCreate(target=url, code=keyword, title=title), ip=client_ip(request))
        except ServiceError as exc:
            return {
                "status": "fail",
                "code": "error:conflict",
                "message": exc.message,
                "errorCode": str(exc.status),
            }
        # 同上：先落库再出网抓标题，别拿写锁等网络
        db.commit()
        db.refresh(link)
        title, source = fetch_title_for(db, link, None)
        if apply_title(link, title, source):
            db.commit()
            db.refresh(link)
        short = f"{_base(request)}/{link.code}"
        return {
            "status": "success",
            "code": "200",
            "message": "success",
            "url": {"keyword": link.code, "url": link.target, "title": link.title or "", "shorturl": short},
        }

    if action == "expand":
        code = _code_from(params.get("shorturl") or "")
        try:
            link = get_link_by_code(db, code)
        except ServiceError as exc:
            return {"status": "fail", "code": "error:notfound", "message": exc.message, "errorCode": "404"}
        return {
            "status": "success",
            "code": "200",
            "keyword": link.code,
            "shorturl": f"{_base(request)}/{link.code}",
            "longurl": link.target,
        }

    if action == "url-stats":
        code = _code_from(params.get("shorturl") or "")
        try:
            link = get_link_by_code(db, code)
            data = get_stats(db, code)
        except ServiceError as exc:
            return {"status": "fail", "code": "error:notfound", "message": exc.message, "errorCode": "404"}
        return {
            "status": "success",
            "code": "200",
            "link": {
                "shorturl": f"{_base(request)}/{link.code}",
                "url": link.target,
                "title": link.title or "",
                "clicks": data["clicks"],
            },
            "stats": {"total": data["clicks"], "unique": data["unique_visitors"]},
        }

    total_links = db.execute(select(func.count()).select_from(Link)).scalar_one() or 0
    total_clicks = db.execute(select(func.coalesce(func.sum(Link.clicks), 0))).scalar_one() or 0
    return {
        "status": "success",
        "code": "200",
        "stats": {"total_links": int(total_links), "total_clicks": int(total_clicks)},
    }


@router.get("", include_in_schema=False)
def yourls_api_get(request: Request, db: Session = Depends(get_db)):
    return yourls_api(request, db)
