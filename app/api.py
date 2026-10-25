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
from app.core.timeutil import ensure_aware, utcnow
from app.core.settings_store import effective
from app.core.apikey import extract_key, touch, verify as verify_api_key
from app.models import ClickEvent, Link
from app.qr import make_qr_png, make_qr_svg
from app.web import branding
from app.schemas import LinkCreate, LinkListOut, LinkOut, LinkRenew, LinkSelfUpdate, LinkUpdate, StatsOut
from app.services import (
    ServiceError,
    all_links_for_export,
    list_tags,
    bulk_update,
    code_available,
    create_link,
    delete_link,
    empty_trash,
    get_link_by_code,
    list_deleted,
    purge_link,
    restore_link,
    get_stats,
    find_similar,
    links_to_csv,
    list_links,
    renew_link,
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


def _self_manage(request: Request, db: Session, code: str, need_alive: bool = True):
    """创建者自助管理的前置校验：管理员直通，否则必须出示这条链接的回执。

    「回执」以前只够看统计，链接过期了想续一天、目标写错了想改一个字母，
    都得去找管理员——开放模式下压根没人可找。这里让回执变成一把真正的钥匙：
    能改、能停、能续命，但只开自己那一扇门。
    """
    link = get_link_by_code(db, code)
    if access.session_tier(request) == access.TIER_ADMIN:
        return link, True
    if not access.may_see(link, request, db):
        raise ServiceError("回执无效或已更换，请用最新那条回执访问", 401)
    if need_alive and link.claim_hash is None:
        raise ServiceError("这条链接的回执已被收回", 401)
    return link, False


@router.get("/links/{code}/self", response_model=LinkOut, summary="回执自助：读取自己那条")
def api_self_get(code: str, request: Request, db: Session = Depends(get_db)):
    """凭回执读自己那条的详情（含是否过期、还剩几天）。"""
    check_api_key(request, db)
    link, _ = _self_manage(request, db, code, need_alive=False)
    out = _serialize(link, request)
    out.expired = link.is_expired
    return out


@router.patch("/links/{code}/self", response_model=LinkOut, summary="回执自助：改目标 / 标题 / 标签 / 过期时间")
def api_self_update(code: str, payload: LinkSelfUpdate, request: Request, db: Session = Depends(get_db)):
    check_api_key(request, db)
    link, is_admin = _self_manage(request, db, code, need_alive=False)
    if not is_admin and link.claim_hash is None:
        raise ServiceError("这条链接的回执已被收回", 401)

    data = payload.model_dump(exclude_unset=True)
    changed = sorted(data.keys())
    if not changed:
        raise ServiceError("没有要修改的字段", 422)
    update_link(db, code, LinkUpdate(**data))
    report.record(
        db,
        "update",
        actor=access_actor(request) if is_admin else "访客",
        code=link.code,
        detail="回执自助修改：" + ", ".join(changed),
        request=request,
    )
    db.commit()
    db.refresh(link)
    out = _serialize(link, request)
    out.expired = link.is_expired
    return out


@router.post("/links/{code}/self/renew", response_model=LinkOut, summary="回执自助：续期并重新启用")
def api_self_renew(code: str, payload: LinkRenew | None = None, request: Request = None, db: Session = Depends(get_db)):
    """过期之后才发现链接死了，是最常见的翻车方式。

    这里给一条「从死里捞回来」的路：过期或停用都能直接续上一段时间，
    顺带把统计累加进一条 notice 里，让后台看得到这条链接被谁救活过。
    """
    check_api_key(request, db)
    link, is_admin = _self_manage(request, db, code, need_alive=False)
    days = (payload.days if payload else 30) or 30
    enabled = payload.enabled if payload else True
    renew_link(db, link, days=days, enabled=enabled)
    report.record(
        db,
        "update",
        actor=access_actor(request) if is_admin else "访客",
        code=link.code,
        detail=f"续期 {days} 天" + ("，并重新启用" if enabled else ""),
        request=request,
    )
    db.commit()
    db.refresh(link)
    out = _serialize(link, request)
    out.expired = link.is_expired
    return out


@router.post("/links/{code}/self/disable", response_model=LinkOut, summary="回执自助：停用或启用")
def api_self_toggle(code: str, request: Request, enabled: bool = True, db: Session = Depends(get_db)):
    check_api_key(request, db)
    link, is_admin = _self_manage(request, db, code)
    update_link(db, code, LinkUpdate(enabled=enabled))
    report.record(
        db,
        "update",
        actor=access_actor(request) if is_admin else "访客",
        code=link.code,
        detail="回执自助" + ("启用" if enabled else "停用"),
        request=request,
    )
    db.commit()
    db.refresh(link)
    out = _serialize(link, request)
    out.expired = link.is_expired
    return out


@router.delete("/links/{code}/self", summary="回执自助：删除自己那条")
def api_self_delete(code: str, request: Request, db: Session = Depends(get_db)):
    """访客删自己建的那条。

    以前访客建错一条就永久留在库里的——既没有回执删除，也找不到管理员。
    这里凭回执照删，助手库里立刻消失；回执本身也随链接一起作废。
    """
    check_api_key(request, db)
    link, is_admin = _self_manage(request, db, code)
    delete_link(db, code)
    report.record(
        db, "delete", actor=access_actor(request) if is_admin else "访客", code=code, detail="回执自助删除", request=request
    )
    db.commit()
    return {"ok": True, "message": f"已删除 /{code}"}


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


@router.get("/codes/{code}/available", summary="自定义短码可用性")
def api_code_available(code: str, request: Request, db: Session = Depends(get_db)):
    """创建前顺手查一下短码能不能用。

    只在「这个请求本来就有创建权」时才说真话；否则一律回可以用——
    否则这就是个免费的短码库扫描器，谁都能顺着把别人的短码摸出来。
    """
    if not _access_granted(request):
        raise ServiceError("访问受限：请先输入访问令牌", 403)
    if not can_create(request, db):
        return {"code": code, "available": True, "message": ""}
    ok, msg = code_available(db, code)
    return {"code": code, "available": ok, "message": msg}


@router.get("/duplicate", summary="按目标地址查重")
def api_duplicate(request: Request, target: str = Query(..., min_length=1), db: Session = Depends(get_db)):
    """这个地址是不是已经收录过了。

    只给管理员：结果里有别人建的短码，开放给访客等于把链接库开个后门。
    """
    check_api_key(request, db)
    require_manage(request, db)
    return {"items": find_similar(db, target)}


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
    body = "﻿" + links_to_csv(links, base_url=str(request.base_url).rstrip("/"))
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="simpleurl-links.csv"'},
    )


@router.post("/links", response_model=LinkOut, status_code=201, summary="创建短链接")
def api_create_link(payload: LinkCreate, request: Request, db: Session = Depends(get_db)):
    check_api_key(request, db)
    require_create(request, db)

    # 同一地址已经收录过时，先问一句「要不要复用」。
    # 默认行为没变（照样新建一条），但要了 reuse 就直接把现成那条还回去，
    # 排队场景里少一堆 a1B2 / a1B3 这种只差一位的重复短码。
    reused = None
    if payload.reuse:
        for item in find_similar(db, payload.target, limit=1):
            reused = db.execute(select(Link).where(Link.code == item["code"])).scalar_one_or_none()
            if reused is not None:
                break

    if reused is not None:
        link = reused
        existed = True
    else:
        link = create_link(db, payload, ip=client_ip(request))
        existed = False

    is_admin = access.is_admin(request, db)
    token = None
    if not is_admin:
        # 线索只在开放模式下有意义：管理员不需要靠它找回任何东西
        if payload.clue and settings.claim_lookup:
            claim.register_clue(db, link, payload.clue)
        token = claim.reissue(db, link) if existed else claim.issue(db, link)
        request.state.claim_token = token
    report.record(
        db,
        "create",
        actor="admin" if is_admin else "访客",
        code=link.code,
        detail=("复用已有短码 target=" if existed else "target=") + link.target[:200],
        request=request,
    )
    db.commit()
    db.refresh(link)
    out = _serialize(link, request, token)
    out.reused = existed
    return out


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
    return {"ok": True, "message": f"已删除 {code}（已进回收站，30 天内可恢复）"}


# ---------- 回收站 ----------
# 删除改成软删之后，还得有一条「真的删掉」的路，否则回收站只是换个地方堆垃圾。
# 三个动作：看、捞回来、彻底清。


@router.get("/trash", summary="回收站列表")
def api_trash(
    request: Request,
    q: str | None = Query(None, description="搜短码 / 目标 / 标题"),
    db: Session = Depends(get_db),
):
    check_api_key(request, db)
    require_manage(request, db)
    from app.services import TRASH_RETENTION_DAYS

    items = []
    for link in list_deleted(db, q=q):
        row = link.to_dict()
        deleted = ensure_aware(link.deleted_at)
        row["days_left"] = max(0, TRASH_RETENTION_DAYS - (utcnow() - deleted).days) if deleted else 0
        items.append(row)
    return {"items": items, "retention_days": TRASH_RETENTION_DAYS}


@router.post("/trash/{code}/restore", summary="从回收站恢复")
def api_trash_restore(code: str, request: Request, db: Session = Depends(get_db)):
    check_api_key(request, db)
    require_manage(request, db)
    link = restore_link(db, code)
    report.record(db, "restore", actor=access_actor(request), code=link.code, detail="从回收站恢复", request=request)
    db.commit()
    db.refresh(link)
    return {"ok": True, "message": f"已恢复 /{link.code}", "item": _serialize(link, request).model_dump()}


@router.delete("/trash/{code}", summary="彻底删除（不可撤销）")
def api_trash_purge(code: str, request: Request, db: Session = Depends(get_db)):
    check_api_key(request, db)
    require_manage(request, db)
    purge_link(db, code)
    report.record(db, "purge", actor=access_actor(request), code=code, detail="彻底删除", request=request)
    db.commit()
    return {"ok": True, "message": f"/{code} 已彻底删除"}


@router.post("/trash/empty", summary="清空回收站")
def api_trash_empty(request: Request, db: Session = Depends(get_db)):
    check_api_key(request, db)
    require_manage(request, db)
    removed = empty_trash(db)
    report.record(db, "purge", actor=access_actor(request), detail=f"清空回收站 {removed} 条", request=request)
    db.commit()
    return {"ok": True, "removed": removed, "message": f"已清空回收站，{removed} 条彻底删除"}


@router.post("/links/bulk", summary="批量启用 / 停用 / 删除")
def api_bulk(request: Request, payload: dict, db: Session = Depends(get_db)):
    check_api_key(request, db)
    require_manage(request, db)
    codes = payload.get("codes") or []
    if isinstance(codes, str):
        codes = [c for c in codes.replace(",", "\n").split() if c]
    action = str(payload.get("action") or "")
    days = int(payload.get("days") or 30)
    tag = str(payload.get("tag") or "")
    result = bulk_update(db, list(codes), action, days=days, tag=tag)
    labels = {"enable": "启用", "disable": "停用", "delete": "删除", "renew": "续期", "tag": "打标签"}
    label = labels.get(action, action)
    report.record(
        db,
        "delete" if action == "delete" else "update",
        actor=access_actor(request),
        detail=f"批量{label} {result['affected']} 条",
        request=request,
    )
    db.commit()
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


@router.get("/qr/{code}", summary="二维码图片")
def api_qr(
    code: str,
    request: Request,
    size: int = Query(240, description="边长像素，会就近取一档"),
    fmt: str = Query("png", pattern="^(png|svg)$", description="png 或 svg"),
    logo: int = Query(0, ge=0, le=1, description="1 表示中间带品牌图标"),
    download: int = Query(0, ge=0, le=1, description="1 表示按附件下载"),
    db: Session = Depends(get_db),
):
    """二维码：尺寸、格式、中间图标都能挑。

    以前固定 240px PNG，贴海报就得自己放大到模糊。SVG 给印刷和深色底用，
    矢量放大不糊；带 logo 的版本错误纠正开到 H，挖掉 20% 照样扫得出来。
    """
    check_api_key(request, db)
    link = get_link_by_code(db, code)
    if not access.may_see(link, request, db):
        require_manage(request, db)
    url = str(request.base_url).rstrip("/") + "/" + link.code
    from app.qr import nearest_size

    target_size = nearest_size(size)
    headers = {"Cache-Control": "public, max-age=3600"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="simpleurl-{link.code}.{fmt}"'
    if fmt == "svg":
        return Response(content=make_qr_svg(url, logo=bool(logo)), media_type="image/svg+xml", headers=headers)
    return Response(
        content=make_qr_png(url, pixel_size=target_size, logo=bool(logo)),
        media_type="image/png",
        headers=headers,
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


def can_create(request: Request, db: Session) -> bool:
    """当前请求是否具备创建权，只问不抛。

    `require_create` 是同一套判断的「拒绝版」：要给接口做能力探测时用这个，
    别去 catch 那个异常——异常里带着给人看的中文提示，拿来当布尔值用很脆。
    """
    try:
        require_create(request, db)
    except ServiceError:
        return False
    return True


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


# ---------- 找回回执 ----------


@router.post("/claim/recover", summary="凭线索找回回执")
def api_claim_recover(payload: dict, request: Request, db: Session = Depends(get_db)):
    """丢了回执？留过线索就还能捞回来。

    三道闸门，缺一不可：
    1. 线索必须与登记时一致（存的是哈希，比对不泄露原文）
    2. 同一 IP 1 小时只有 3 次机会
    3. 只重签**最近一条**链接的回执，不是「你名下全部」

    换句话说：知道你的邮箱，最多碰运气看到你最近建的那一条。
    这比「丢了就彻底没救」好，也比「凭邮箱列出全部短链」安全得多。
    """
    if not settings.claim_lookup:
        raise ServiceError("站点未开放回执找回", 403)
    clue = str(payload.get("clue") or "").strip()
    if not clue:
        raise ServiceError("请填写你创建链接时留下的线索", 422)
    ip = client_ip(request)
    if not claim.lookup_allowed(ip):
        raise ServiceError("找回次数过多，请 1 小时后再试", 429)
    link = claim.lookup(db, clue)
    if link is None:
        # 不区分「线索不存在」和「线索对但链接没了」：后者等于确认了这条线索在用
        raise ServiceError("没有找到对应的链接", 404)
    if link.is_expired:
        raise ServiceError("这条链接已经过期，回执也一并失效了", 410)
    token = claim.reissue(db, link)
    request.state.claim_token = token
    report.record(db, "claim", actor="访客", code=link.code, detail="凭线索找回回执", request=request)
    db.commit()
    base = str(request.base_url).rstrip("/")
    return {
        "ok": True,
        "code": link.code,
        "title": link.title,
        "short_url": f"{base}/{link.code}",
        "claim_token": token,
        "stats_url": f"{base}/s/{link.code}?t={token}",
        "message": "找到了，这是新的回执链接",
    }
