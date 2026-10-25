"""REST API 路由。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import access, claim, report
from app.core.admin import current_admin
from app.core.config import settings
from app.core.db import get_db
from app.core.security import client_ip
from app.core.settings_store import effective
from app.core.apikey import extract_key, touch, verify as verify_api_key
from app.models import ClickEvent, Link
from app.qr import make_qr_png
from app.web import branding
from app.schemas import LinkCreate, LinkListOut, LinkOut, LinkUpdate, StatsOut
from app.services import (
    ServiceError,
    all_links_for_export,
    list_tags,
    bulk_update,
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


def _serialize(link: Link, request: Request, claim_token: str | None = None) -> LinkOut:
    out = LinkOut.model_validate(link.to_dict())
    base = str(request.base_url).rstrip("/")
    out.short_url = base + "/" + link.code
    out.stats_url = f"{base}/s/{link.code}"
    if claim_token:
        out.claim_token = claim_token
        out.stats_url = f"{base}/s/{link.code}?t={claim_token}"
    return out


def check_api_key(request: Request, db: Session) -> None:
    """开启 REQUIRE_API_KEY 后校验密钥；顺手更新最后使用时间。"""
    key = extract_key(request)
    if not verify_api_key(request, db):
        raise ServiceError("API 密钥缺失或无效，请在请求头带 X-Api-Key", 401)
    if key:
        touch(db, key)
        db.commit()


def access_actor(request: Request) -> str:
    """留痕里的操作者：管理员会话名，或匿名访客。"""
    admin = current_admin(request)
    return admin or "匿名"


def require_create(request: Request, db: Session) -> None:
    """创建准入：开放模式人人可建，admin 模式只给管理员。"""
    if access.is_admin(request, db):
        return
    if access.locked_by_weak_token():
        raise ServiceError("站点口令过弱，创建功能已锁定，请管理员更换 ADMIN_TOKEN", 403)
    if not access.is_admin_mode(db):
        return
    raise ServiceError("站点已设为仅管理员可使用，请先登录后台", 401)


def require_manage(request: Request, db: Session) -> None:
    """浏览 / 导出 / 统计准入：任何模式都只给管理员。

    放开的话，链接库就是一份公开的「谁在用什么短链」清单——
    开放模式只开放「建」，不开放「看别人的」。
    """
    if access.is_admin(request, db):
        return
    if access.is_admin_mode(db):
        raise ServiceError("站点已设为仅管理员可使用，请先登录后台", 401)
    raise ServiceError("链接库与统计数据仅管理员可见，请先登录后台", 401)


@router.get("/links", response_model=LinkListOut, summary="短链接列表")
def api_list_links(
    request: Request,
    q: str | None = Query(None, description="搜索短码/目标/标题/标签"),
    tag: str | None = None,
    status: str | None = Query(None, pattern="^(enabled|disabled|expired|expiring)$"),
    sort: str = Query("created_desc", pattern="^(created_desc|created_asc|clicks_desc|clicks_asc|code_asc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    check_api_key(request, db)
    require_manage(request, db)
    total, items = list_links(db, q=q, tag=tag, status=status, sort=sort, page=page, page_size=page_size)
    return LinkListOut(
        total=total, page=page, page_size=page_size, items=[_serialize(item, request) for item in items]
    )


@router.get("/tags", summary="标签列表")
def api_tags(request: Request, db: Session = Depends(get_db)):
    """供前后台的标签筛选下拉使用。

    标签本身不含隐私（不给目标地址、不给点击数），所以访客也读得到；
    但要先过访问令牌那道门，站点设了门禁就别绕过它。
    """
    check_api_key(request, db)
    if not _access_granted(request):
        raise ServiceError("访问受限：请先输入访问令牌", 403)
    return {"items": list_tags(db)}


@router.get("/links/export", summary="导出 CSV")
def api_export(
    request: Request,
    q: str | None = None,
    tag: str | None = None,
    status: str | None = Query(None, pattern="^(enabled|disabled|expired|expiring)$"),
    sort: str = Query("created_desc", pattern="^(created_desc|created_asc|clicks_desc|clicks_asc|code_asc)$"),
    db: Session = Depends(get_db),
):
    check_api_key(request, db)
    require_manage(request, db)
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
    require_create(request, db)
    link = create_link(db, payload, ip=client_ip(request))
    # 开放模式下创建者拿不到链接库，给一张一次性回执，凭它回看自己这条
    is_admin = access.is_admin(request, db)
    token = None if is_admin else claim.issue(db, link)
    report.record(
        db,
        "create",
        actor="admin" if is_admin else "访客",
        code=link.code,
        detail=f"target={link.target[:200]}",
        request=request,
    )
    db.commit()
    db.refresh(link)
    return _serialize(link, request, token)


@router.get("/links/{code}", response_model=LinkOut, summary="短链接详情")
def api_get_link(code: str, request: Request, db: Session = Depends(get_db)):
    check_api_key(request, db)
    require_manage(request, db)
    return _serialize(get_link_by_code(db, code), request)


@router.patch("/links/{code}", response_model=LinkOut, summary="更新短链接")
def api_update_link(code: str, payload: LinkUpdate, request: Request, db: Session = Depends(get_db)):
    check_api_key(request, db)
    require_manage(request, db)
    link = update_link(db, code, payload)
    changed = ", ".join(sorted(payload.model_dump(exclude_unset=True).keys()))
    report.record(db, "update", actor=access_actor(request), code=link.code, detail=changed, request=request)
    db.commit()
    db.refresh(link)
    return _serialize(link, request)


@router.delete("/links/{code}", summary="删除短链接")
def api_delete_link(code: str, request: Request, db: Session = Depends(get_db)):
    check_api_key(request, db)
    require_manage(request, db)
    delete_link(db, code)
    report.record(db, "delete", actor=access_actor(request), code=code, request=request)
    db.commit()
    return {"ok": True, "message": f"已删除 {code}"}


@router.post("/links/bulk", summary="批量启用 / 停用 / 删除")
def api_bulk(request: Request, payload: dict, db: Session = Depends(get_db)):
    check_api_key(request, db)
    require_manage(request, db)
    codes = payload.get("codes") or []
    if isinstance(codes, str):
        codes = [c for c in codes.replace(",", "\n").split() if c]
    action = str(payload.get("action") or "")
    result = bulk_update(db, list(codes), action)
    report.record(
        db,
        "delete" if action == "delete" else "update",
        actor=access_actor(request),
        detail=f"批量{'删除' if action == 'delete' else ('启用' if action == 'enable' else '停用')} {result['affected']} 条",
        request=request,
    )
    db.commit()
    label = {"enable": "启用", "disable": "停用", "delete": "删除"}.get(action, action)
    return {**result, "message": f"已{label} {result['affected']} 条"}


@router.get("/links/{code}/receipt", summary="补发回执链接")
def api_reissue_receipt(code: str, request: Request, db: Session = Depends(get_db)):
    """用一次性回执换一张新的回执。

    回执丢了就彻底找不回来（服务端只存哈希），老版本的痛点在这里：管理员
    在后台建的链接根本不发回执，页面却照样渲染一个空的「复制」框；访客把回执
    存丢之后，那条链接就只剩「问管理员」一条路。开放模式又压根没有管理员可问。

    补的办法不是发新凭据，而是**用旧换新**：拿着已有一张有效回执，换一张新的、
    旧的立刻作废。手里没有的那一方永远换不到——安全性不降，可用性回来了。
    """
    check_api_key(request, db)
    link = get_link_by_code(db, code)
    if not access.may_see(link, request, db):
        require_manage(request, db)
        return {"ok": True, "claim_token": None, "message": "管理员无需回执"}
    token = claim.reissue(db, link)
    # 中间件看到这个就把浏览器里那张 cookie 一起换掉
    request.state.claim_token = token
    report.record(db, "claim", actor="访客", code=link.code, detail="回执换新", request=request)
    db.commit()
    base = str(request.base_url).rstrip("/")
    return {"ok": True, "claim_token": token, "stats_url": f"{base}/s/{link.code}?t={token}"}


@router.get("/links/{code}/stats", response_model=StatsOut, summary="访问统计")
def api_link_stats(
    code: str, days: int = Query(30, ge=1, le=365), request: Request = None, db: Session = Depends(get_db)
):
    check_api_key(request, db)
    link = get_link_by_code(db, code)
    # 创建者拿回执（?t=）看自己那一条，其余一律要管理员
    if not access.may_see(link, request, db):
        require_manage(request, db)
    return StatsOut(**get_stats(db, code, days=days))


@router.get("/qr/{code}", summary="二维码 PNG")
def api_qr(code: str, request: Request, db: Session = Depends(get_db)):
    check_api_key(request, db)
    link = get_link_by_code(db, code)
    if not access.may_see(link, request, db):
        require_manage(request, db)
    url = str(request.base_url).rstrip("/") + "/" + link.code
    return Response(
        content=make_qr_png(url),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/access", summary="当前访问模式")
def api_access(request: Request, db: Session = Depends(get_db)):
    # 顺带把「本站是否开了访问令牌、当前这位有没有过门禁」告诉前端，
    # 否则前端只能靠撞一次 403 才知道门在哪儿，加载时会闪一下空列表
    snapshot = access.snapshot(db, request)
    snapshot["access_required"] = bool(settings.access_token)
    snapshot["access_granted"] = _access_granted(request)
    # 品牌名同样以库里的值为准：这里写死 settings.site_name 的话，
    # 后台改了站点名称，页头换了、横幅和提示语还挂着旧名字
    snapshot["site_name"] = branding(db)["site_name"]
    snapshot["announcement"] = str(effective(db, "announcement") or "")
    # 公告与自述都归运行配置，后台可改，前端每次加载都会拿到最新值
    return snapshot


def _access_granted(request: Request) -> bool:
    from app.pages import _check_access

    return _check_access(request)


@router.get("/stats/summary", summary="总览统计")
def api_summary(request: Request, db: Session = Depends(get_db)):
    check_api_key(request, db)
    # 开了访问令牌时先过门禁：门口拦人、屋里却把概况随便读，这门就是摆设
    if not _access_granted(request):
        raise ServiceError("访问受限：请先输入访问令牌", 403)
    if not access.is_admin(request, db):
        # 访客只看站点概况，别把库里有多少条链接当公开情报
        snapshot = access.snapshot(db, request)
        snapshot.update(
            {"total_links": None, "total_clicks": None, "site_name": branding(db)["site_name"]}
        )
        return snapshot
    total_links = db.execute(select(func.count()).select_from(Link)).scalar_one()
    # 被 UA 屏蔽的事件只留痕不计点击，总点击要跟链接列表对得上
    total_clicks = (
        db.execute(select(func.count()).select_from(ClickEvent).where(ClickEvent.blocked.is_(False))).scalar_one()
    )
    return {
        "total_links": int(total_links or 0),
        "total_clicks": int(total_clicks or 0),
        "site_name": branding(db)["site_name"],
        "access_required": bool(settings.access_token),
        **access.snapshot(db, request),
    }
