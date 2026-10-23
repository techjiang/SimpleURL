"""REST API 路由。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import client_ip
from app.core.apikey import extract_key, touch, verify as verify_api_key
from app.models import ClickEvent, Link
from app.qr import make_qr_png
from app.schemas import LinkCreate, LinkListOut, LinkOut, LinkUpdate, StatsOut
from app.services import (
    ServiceError,
    all_links_for_export,
    create_link,
    delete_link,
    get_link_by_code,
    get_stats,
    links_to_csv,
    list_links,
    update_link,
)

logger = logging.getLogger("simpleurl")
router = APIRouter(prefix="/api", tags=["api"])


def _serialize(link: Link, request: Request) -> LinkOut:
    out = LinkOut.model_validate(link.to_dict())
    out.short_url = str(request.base_url).rstrip("/") + "/" + link.code
    return out


def check_api_key(request: Request, db: Session) -> None:
    """开启 REQUIRE_API_KEY 后校验密钥；顺手更新最后使用时间。"""
    key = extract_key(request)
    if not verify_api_key(request, db):
        raise ServiceError("API 密钥缺失或无效，请在请求头带 X-Api-Key", 401)
    if key:
        touch(db, key)
        db.commit()


@router.get("/links", response_model=LinkListOut, summary="短链接列表")
def api_list_links(
    request: Request,
    q: str | None = Query(None, description="搜索短码/目标/标题/标签"),
    tag: str | None = None,
    status: str | None = Query(None, pattern="^(enabled|disabled|expired)$"),
    sort: str = Query("created_desc", pattern="^(created_desc|created_asc|clicks_desc|clicks_asc|code_asc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    check_api_key(request, db)
    total, items = list_links(db, q=q, tag=tag, status=status, sort=sort, page=page, page_size=page_size)
    return LinkListOut(
        total=total, page=page, page_size=page_size, items=[_serialize(item, request) for item in items]
    )


@router.get("/links/export", summary="导出 CSV")
def api_export(
    request: Request,
    q: str | None = None,
    tag: str | None = None,
    status: str | None = Query(None, pattern="^(enabled|disabled|expired)$"),
    sort: str = Query("created_desc", pattern="^(created_desc|created_asc|clicks_desc|clicks_asc|code_asc)$"),
    db: Session = Depends(get_db),
):
    check_api_key(request, db)
    links = all_links_for_export(db, q=q, tag=tag, status=status, sort=sort)
    # 加 BOM，Excel 打开中文不乱码
    body = "﻿" + links_to_csv(links)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="simpleurl-links.csv"'},
    )


@router.post("/links", response_model=LinkOut, status_code=201, summary="创建短链接")
def api_create_link(payload: LinkCreate, request: Request, db: Session = Depends(get_db)):
    check_api_key(request, db)
    link = create_link(db, payload, ip=client_ip(request))
    db.commit()
    db.refresh(link)
    return _serialize(link, request)


@router.get("/links/{code}", response_model=LinkOut, summary="短链接详情")
def api_get_link(code: str, request: Request, db: Session = Depends(get_db)):
    check_api_key(request, db)
    return _serialize(get_link_by_code(db, code), request)


@router.patch("/links/{code}", response_model=LinkOut, summary="更新短链接")
def api_update_link(code: str, payload: LinkUpdate, request: Request, db: Session = Depends(get_db)):
    check_api_key(request, db)
    link = update_link(db, code, payload)
    db.commit()
    db.refresh(link)
    return _serialize(link, request)


@router.delete("/links/{code}", summary="删除短链接")
def api_delete_link(code: str, request: Request, db: Session = Depends(get_db)):
    check_api_key(request, db)
    delete_link(db, code)
    db.commit()
    return {"ok": True, "message": f"已删除 {code}"}


@router.get("/links/{code}/stats", response_model=StatsOut, summary="访问统计")
def api_link_stats(
    code: str, days: int = Query(30, ge=1, le=365), request: Request = None, db: Session = Depends(get_db)
):
    check_api_key(request, db)
    return StatsOut(**get_stats(db, code, days=days))


@router.get("/qr/{code}", summary="二维码 PNG")
def api_qr(code: str, request: Request, db: Session = Depends(get_db)):
    check_api_key(request, db)
    link = get_link_by_code(db, code)
    url = str(request.base_url).rstrip("/") + "/" + link.code
    return Response(
        content=make_qr_png(url),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/stats/summary", summary="总览统计")
def api_summary(request: Request, db: Session = Depends(get_db)):
    check_api_key(request, db)
    total_links = db.execute(select(func.count()).select_from(Link)).scalar_one()
    # 被 UA 屏蔽的事件只留痕不计点击，总点击要跟链接列表对得上
    total_clicks = (
        db.execute(select(func.count()).select_from(ClickEvent).where(ClickEvent.blocked.is_(False))).scalar_one()
    )
    return {
        "total_links": int(total_links or 0),
        "total_clicks": int(total_clicks or 0),
        "site_name": settings.site_name,
        "access_required": bool(settings.access_token),
    }
