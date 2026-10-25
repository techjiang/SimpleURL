"""管理后台 API：登录、仪表盘、设置、UA 屏蔽、API 密钥、数据迁移。"""

from __future__ import annotations

import datetime as dt
import logging
import secrets

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core import access, guard, policy, report, social
from app.core.admin import (
    COOKIE_NAME,
    check_credentials,
    clear_cookie_value,
    current_admin,
    is_admin_enabled,
    login_response_value,
    make_token,
)
from app.core.clicks import click_buffer
from app.core.config import settings
from app.core.db import get_db
from app.core.security import client_ip, rate_limiter
from app.core.settings_store import EDITABLE, code_length_value, effective, get_all, set_value
from app.core.yourls import parse as parse_yourls
from app.models import ApiKey, ClickEvent, Link, Setting
from app.schemas import (
    ApiKeyCreate,
    ApiKeyOut,
    DashboardOut,
    MigrationIn,
    MigrationOut,
    MessageOut,
    PlatformRuleOut,
    SettingsOut,
    SettingsUpdate,
    UaRuleOut,
)
from app.services import ServiceError, flush_clicks, import_yourls, runtime_status

logger = logging.getLogger("simpleurl")
router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin(request: Request) -> str:
    admin = current_admin(request)
    if admin is None:
        raise ServiceError("未登录或会话已过期", 401)
    return admin


def _session_cookie(token: str) -> dict:
    return {COOKIE_NAME: token}


@router.get("/session", summary="当前登录态")
def api_session(request: Request):
    admin = current_admin(request)
    return {"ok": True, "admin": admin, "enabled": is_admin_enabled()}


@router.post("/login", summary="后台登录")
def api_login(payload: dict, response: Response, request: Request):
    if not is_admin_enabled():
        raise ServiceError("未配置 ADMIN_TOKEN，后台不可用", 403)
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    # 登录按 IP 限速：挡掉暴力尝试，也不把账号是否存在漏给攻击者
    if not rate_limiter.allow(f"login:{client_ip(request)}", settings.login_rate_limit, 300):
        raise ServiceError("尝试次数过多，请 5 分钟后再试", 429)
    if not check_credentials(username, password):
        raise ServiceError("账号或密码不对", 401)
    response.set_cookie(
        COOKIE_NAME,
        make_token(username),
        max_age=settings.session_ttl,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"ok": True, "message": f"欢迎回来，{username}"}


@router.post("/logout", summary="退出登录")
def api_logout(response: Response):
    value = clear_cookie_value()
    response.set_cookie(COOKIE_NAME, "", max_age=0, httponly=True, samesite="lax", path="/")
    return {"ok": True, "message": "已退出"}


@router.get("/dashboard", response_model=DashboardOut, summary="仪表盘")
def api_dashboard(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()

    total_links = db.execute(select(func.count()).select_from(Link)).scalar_one() or 0
    total_clicks = db.execute(select(func.coalesce(func.sum(Link.clicks), 0))).scalar_one() or 0
    enabled = db.execute(select(func.count()).select_from(Link).where(Link.enabled.is_(True))).scalar_one() or 0
    disabled = total_links - enabled
    expired = (
        db.execute(
            select(func.count()).select_from(Link).where(Link.expires_at.is_not(None), Link.expires_at <= now)
        ).scalar_one()
        or 0
    )
    expiring_horizon = now + dt.timedelta(days=7)
    expiring_rows = list(
        db.execute(
            select(Link.code, Link.target, Link.expires_at)
            .where(Link.expires_at.is_not(None), Link.expires_at > now, Link.expires_at <= expiring_horizon)
            .order_by(Link.expires_at.asc())
            .limit(8)
        ).all()
    )
    expiring_links = (
        db.execute(
            select(func.count())
            .select_from(Link)
            .where(Link.expires_at.is_not(None), Link.expires_at > now, Link.expires_at <= expiring_horizon)
        ).scalar_one()
        or 0
    )

    blocked_events = (
        db.execute(select(func.count()).select_from(ClickEvent).where(ClickEvent.blocked.is_(True))).scalar_one() or 0
    )
    clicks_today = (
        db.execute(
            select(func.count()).select_from(ClickEvent).where(func.date(ClickEvent.ts) == today.isoformat())
        ).scalar_one()
        or 0
    )

    top_links = [
        {"code": code, "target": target, "clicks": int(clicks or 0)}
        for code, target, clicks in db.execute(
            select(Link.code, Link.target, Link.clicks).order_by(Link.clicks.desc()).limit(8)
        ).all()
    ]

    recent = [
        {
            "code": code,
            "device": device or "未知",
            "blocked": bool(blocked),
            "ts": ensure_iso(ts),
            "referrer": (referrer or "")[:80] or None,
            "platform": platform_key or "",
            "platform_label": social.label(platform_key),
        }
        for code, device, blocked, ts, referrer, platform_key in db.execute(
            select(
                Link.code, ClickEvent.device, ClickEvent.blocked,
                ClickEvent.ts, ClickEvent.referrer, ClickEvent.platform,
            )
            .join(Link, Link.id == ClickEvent.link_id)
            .order_by(ClickEvent.ts.desc())
            .limit(12)
        ).all()
    ]

    daily_rows = (
        db.execute(
            select(func.date(ClickEvent.ts), func.count(ClickEvent.id))
            .where(ClickEvent.ts >= now - dt.timedelta(days=13))
            .group_by(func.date(ClickEvent.ts))
            .order_by(func.date(ClickEvent.ts))
        )
        .all()
    )
    buckets: dict[str, int] = {}
    for i in range(14):
        buckets[(now - dt.timedelta(days=13 - i)).date().isoformat()] = 0
    for d, c in daily_rows:
        key = d.isoformat() if hasattr(d, "isoformat") else str(d)
        buckets[key] = int(c)

    return DashboardOut(
        total_links=int(total_links),
        total_clicks=int(total_clicks),
        enabled_links=int(enabled),
        disabled_links=int(disabled),
        expired_links=int(expired),
        expiring_links=int(expiring_links),
        expiring=[
            {"code": code, "target": target, "expires_at": ensure_iso(expires_at)}
            for code, target, expires_at in expiring_rows
        ],
        pending_clicks=click_buffer.pending(),
        blocked_events=int(blocked_events),
        clicks_today=int(clicks_today),
        top_links=top_links,
        recent_clicks=recent,
        daily=[{"date": k, "count": v} for k, v in buckets.items()],
    )


def ensure_iso(value) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.isoformat()


@router.get("/settings", response_model=SettingsOut, summary="读取设置")
def api_get_settings(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    values = get_all(db)
    return SettingsOut(
        site_name=str(values.get("site_name") or settings.site_name),
        redirect_rate_limit=int(values.get("redirect_rate_limit") or 0),
        click_daily_limit=int(values.get("click_daily_limit") or 0),
        fetch_title=bool(values.get("fetch_title", True)),
        require_api_key=bool(values.get("require_api_key", False)),
        create_daily_limit=int(values.get("create_daily_limit") or 0),
        code_length=code_length_value(db),
        click_flush_interval=settings.click_flush_interval,
        pending_clicks=click_buffer.pending(),
        admin_enabled=is_admin_enabled(),
        access_required=bool(settings.access_token),
        access_mode=access.current_mode(db),
        access_mode_label=access.LABELS.get(access.current_mode(db), ""),
        access_locked=access.is_admin_mode(db),
        announcement=str(values.get("announcement") or ""),
        social_guard=bool(values.get("social_guard", True)),
        social_block_hint=str(values.get("social_block_hint") or ""),
        social_tag_qq=bool(values.get("social_tag_qq", True)),
        social_tag_wechat=bool(values.get("social_tag_wechat", True)),
    )


@router.post("/settings", summary="保存设置")
def api_save_settings(payload: SettingsUpdate, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        if key not in EDITABLE:
            continue
        if key in {"redirect_rate_limit", "click_daily_limit", "create_daily_limit"}:
            value = max(0, int(value))
        if key == "code_length":
            value = max(3, min(16, int(value)))
        if key == "access_mode":
            value = access.normalize_mode(value)
        set_value(db, key, value)
    policy.invalidate()
    report.record(db, "settings", actor=require_admin(request), detail=", ".join(sorted(data.keys())), request=request)
    db.commit()
    return {"ok": True, "message": "已保存"}


@router.post("/settings/reset", summary="恢复默认设置")
def api_reset_settings(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    for key in EDITABLE:
        row = db.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
        if row is not None:
            db.delete(row)
    policy.invalidate()
    report.record(db, "settings", actor=require_admin(request), detail="恢复默认设置", request=request)
    db.commit()
    return {"ok": True, "message": "已恢复环境变量默认值"}


@router.get("/ua-rules", response_model=UaRuleOut, summary="UA 屏蔽规则")
def api_get_ua_rules(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    return UaRuleOut(rules=guard.blocked_ua_rules(db))


@router.post("/ua-rules", response_model=UaRuleOut, summary="保存 UA 屏蔽规则")
def api_save_ua_rules(payload: dict, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    rules = guard.parse_list(str(payload.get("rules") or ""))
    guard.save_ua_rules(db, rules)
    report.record(db, "settings", actor=require_admin(request), detail=f"UA 屏蔽规则 {len(rules)} 条", request=request)
    db.commit()
    return UaRuleOut(rules=guard.blocked_ua_rules(db))


@router.get("/social-rules", response_model=PlatformRuleOut, summary="社交平台防洪策略")
def api_get_social_rules(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    rules = guard.platform_rules(db)
    return PlatformRuleOut(
        rules=rules,
        hint=guard.platform_notice(db),
        platforms=[
            {"key": key, "label": name, "strategy": rules.get(key, social.DEFAULT_STRATEGY)}
            for key, (name, _) in social.PLATFORMS.items()
        ],
    )


@router.post("/social-rules", response_model=PlatformRuleOut, summary="保存社交平台防洪策略")
def api_save_social_rules(payload: dict, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    raw_rules = payload.get("rules")
    if isinstance(raw_rules, str):
        rules = social.strategies_from_text(raw_rules)
    elif isinstance(raw_rules, dict):
        rules = {k: social.normalize_strategy(v) for k, v in raw_rules.items() if social.known(k)}
    else:
        rules = {}
    guard.save_platform_rules(db, rules)
    policy.invalidate()
    report.record(db, "settings", actor=require_admin(request), detail="社交平台防洪策略", request=request)
    if "hint" in payload:
        guard.save_platform_notice(db, str(payload.get("hint") or ""))
    db.commit()
    return api_get_social_rules(request, db)


@router.get("/platform-stats", summary="社交平台访问分布")
def api_platform_stats(request: Request, days: int = Query(14, ge=1, le=365), db: Session = Depends(get_db)):
    require_admin(request)
    items = guard.platform_hit_stats(db, days)
    return {"days": days, "items": items, "total": sum(i["count"] + i["blocked"] for i in items)}


@router.get("/blocked-events", summary="最近被防洪规则拦下的访问")
def api_blocked_events(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    platform: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """拦截留痕：平台、时间、UA 摘要，不含 IP 明文（只存哈希也一并省略）。"""
    require_admin(request)
    stmt = (
        select(Link.code, ClickEvent.platform, ClickEvent.ua, ClickEvent.ts)
        .join(Link, Link.id == ClickEvent.link_id)
        .where(ClickEvent.blocked.is_(True))
        .order_by(ClickEvent.ts.desc())
        .limit(limit)
    )
    if platform and social.known(platform):
        stmt = stmt.where(ClickEvent.platform == platform)
    items = [
        {
            "code": code,
            "platform": platform_key or "",
            "label": social.label(platform_key),
            "ua": (ua or "")[:90],
            "ts": ensure_iso(ts),
        }
        for code, platform_key, ua, ts in db.execute(stmt).all()
    ]
    return {"items": items, "total": len(items)}


@router.get("/runtime", summary="运行状态")
def api_runtime(request: Request, db: Session = Depends(get_db)):
    """升级、排障先看这里：库多大、排队多少、丢了什么、哪些链接快到期。"""
    require_admin(request)
    return runtime_status(db)


@router.post("/prune-events", summary="清理过期点击明细")
def api_prune_events(
    request: Request, days: int = Query(0, ge=0, le=3650), db: Session = Depends(get_db)
):
    """清掉保留期之前的点击明细。累计点击数不动，统计口径不变。"""
    require_admin(request)
    from app.services import CLICK_EVENT_RETENTION_DAYS, prune_click_events

    keep = days or CLICK_EVENT_RETENTION_DAYS
    deleted = prune_click_events(db, keep)
    report.record(db, "settings", actor=require_admin(request), detail=f"清理 {keep} 天前点击明细 {deleted} 条", request=request)
    db.commit()
    return {"ok": True, "deleted": deleted, "days": keep}


@router.get("/audit", summary="操作留痕")
def api_audit(
    request: Request,
    limit: int = Query(40, ge=1, le=200),
    action: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """最近的写操作：谁、什么时候、对哪条链接做了什么。"""
    require_admin(request)
    items = report.recent(db, limit=limit, action=action or None)
    return {"items": items, "actions": [{"key": k, "label": v} for k, v in report.ACTIONS.items()]}


@router.get("/api-keys", response_model=list[ApiKeyOut], summary="API 密钥列表")
def api_list_keys(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    rows = db.execute(select(ApiKey).order_by(ApiKey.id.desc())).scalars().all()
    return [ApiKeyOut(**row.to_dict()) for row in rows]


@router.post("/api-keys", response_model=ApiKeyOut, status_code=201, summary="新建 API 密钥")
def api_create_key(payload: ApiKeyCreate, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    key = ApiKey(name=payload.name or "默认密钥", key="sk-" + secrets.token_urlsafe(24))
    db.add(key)
    db.commit()
    db.refresh(key)
    return ApiKeyOut(**key.to_dict())


@router.delete("/api-keys/{key_id}", summary="删除 API 密钥")
def api_delete_key(key_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    row = db.get(ApiKey, key_id)
    if row is None:
        raise ServiceError("密钥不存在", 404)
    db.delete(row)
    db.commit()
    return {"ok": True, "message": "已删除"}


@router.post("/flush-clicks", summary="立即结算延迟计数")
def api_flush_clicks(request: Request):
    require_admin(request)
    written = flush_clicks()
    return {"ok": True, "written": written, "message": f"已结算 {written} 条点击"}


@router.post("/migrate/yourls", response_model=MigrationOut, summary="从 YOURLS 迁移")
def api_migrate(payload: MigrationIn, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    try:
        rows = parse_yourls(payload.payload)
    except ValueError as exc:
        raise ServiceError(str(exc), 422) from exc
    if not rows:
        raise ServiceError("没解析出任何记录，检查一下粘贴的内容", 422)
    result = import_yourls(db, rows)
    report.record(
        db,
        "import",
        actor=require_admin(request),
        detail=f"导入 {result['imported']} 条，换码 {result['renamed']} 条，失败 {result['failed']} 条",
        request=request,
    )
    db.commit()
    return MigrationOut(**result)
