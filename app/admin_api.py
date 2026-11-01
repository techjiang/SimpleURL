"""管理后台 API：登录、仪表盘、设置、UA 屏蔽、API 密钥、数据迁移。"""

from __future__ import annotations

import datetime as dt
import logging
import os
import secrets
import shutil
import tempfile

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core import access, guard, policy, report, social
from app.core.admin import (
    COOKIE_NAME,
    check_credentials,
    env_file_path as admin_env_file_path,
    write_env_vars,
    clear_cookie_value,
    current_admin,
    is_admin_enabled,
    login_response_value,
    make_token,
)
from app.core.clicks import click_buffer
from app.core.config import settings
from app.core.db import get_db, get_engine, session_scope
from app.core.security import client_ip, rate_limiter
from app.core.timeutil import local_day_sql, local_day_start, today_local
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
from app.services import NO_CLICK_LABELS, ServiceError, flush_clicks, import_yourls, runtime_status

logger = logging.getLogger("simpleurl")
# 还原包上限：整个库塞进内存里验一遍，太大就不该走这条路（改用 simpleurl restore，
# 那条路是流式的）。512 MB 够绝大多数自托管站点。
MAX_RESTORE_BYTES = 512 * 1024 * 1024

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin(request: Request) -> str:
    """普通管理员：读数据、改链接、改配置。

    API 密钥在这里**不算数**。原来 `/api/admin/*` 完全不看 `REQUIRE_API_KEY`，
    反过来也没人校验密钥与会话的边界——一把为了「让脚本建短链」而签发的密钥，
    能直接读仪表盘、导出全站链接、改设置。密钥泄露的后果不该等于后台失守。
    """
    if access.session_tier(request) != access.TIER_ADMIN:
        admin = current_admin(request)
        if admin is not None:
            raise ServiceError("当前会话只有配置权限，请用管理员账号重新登录", 401)
        raise ServiceError("未登录或会话已过期", 401)
    return current_admin(request) or "admin"


def require_settings_admin(request: Request) -> str:
    """改配置需要哪一级：管理员，或令牌级会话（换口令、撤密钥的救场通道）。"""
    actor = current_admin(request)
    if not access.can_manage_settings(request) or actor is None:
        raise ServiceError("未登录或会话已过期", 401)
    return actor


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
    # 「今日」一律指本地自然日，和统计页、限额判定同一口径。
    # 以前这里写 `now.astimezone().date()`：取的是**系统**时区，容器里通常是 UTC，
    # 于是「TZ=Asia/Shanghai + 系统跑 UTC」这个最常见的组合下，仪表盘的
    # 「今日点击」在本地 00:00~08:00 之间全程挂在昨天，柱状图最后一根也是空的。
    today = today_local()

    total_links = db.execute(select(func.count()).select_from(Link)).scalar_one() or 0
    total_clicks = db.execute(select(func.coalesce(func.sum(Link.clicks), 0))).scalar_one() or 0
    # 回收站里的链接不算「链接总数」「已启用」「已停用」「已过期」。
    # 以前整个库一起数：仪表盘写「链接总数 50」，链接列表里只有 47 条，
    # 差的那 3 条在回收站——数字对不上，人就开始怀疑哪边坏了。
    alive = Link.deleted_at.is_(None)
    live = db.execute(select(func.count()).select_from(Link).where(alive)).scalar_one() or 0
    total_links = int(live)
    enabled = db.execute(select(func.count()).select_from(Link).where(alive, Link.enabled.is_(True))).scalar_one() or 0
    disabled = total_links - enabled
    expired = (
        db.execute(
            select(func.count())
            .select_from(Link)
            .where(alive, Link.expires_at.is_not(None), Link.expires_at <= now)
        ).scalar_one()
        or 0
    )
    trashed = int(
        db.execute(select(func.count()).select_from(Link).where(Link.deleted_at.is_not(None))).scalar_one() or 0
    )
    expiring_horizon = now + dt.timedelta(days=7)
    expiring_rows = list(
        db.execute(
            select(Link.code, Link.target, Link.expires_at)
            .where(alive, Link.expires_at.is_not(None), Link.expires_at > now, Link.expires_at <= expiring_horizon)
            .order_by(Link.expires_at.asc())
            .limit(8)
        ).all()
    )
    expiring_links = (
        db.execute(
            select(func.count())
            .select_from(Link)
            .where(alive, Link.expires_at.is_not(None), Link.expires_at > now, Link.expires_at <= expiring_horizon)
        ).scalar_one()
        or 0
    )

    # 「被留痕」不等于「被拦下」：UA 屏蔽算拦截，社交平台的「放行不计数」
    # 用户其实打开了页面。混在一个数字里，仪表盘和链接列表永远对不上账。
    blocked_events = (
        db.execute(select(func.count()).select_from(ClickEvent).where(ClickEvent.blocked.is_(True))).scalar_one() or 0
    )
    # 没计入的访问按原因拆开。只给一个总数的话，看到「拦截留痕 40」根本
    # 判断不了是有人在刷（该调限速）还是 UA 黑名单误伤了正常用户（该改规则）。
    blocked_by_reason = {
        str(key or ""): int(count)
        for key, count in db.execute(
            select(ClickEvent.block_reason, func.count(ClickEvent.id))
            .where(ClickEvent.blocked.is_(True))
            .group_by(ClickEvent.block_reason)
        ).all()
    }
    no_click = [
        {"reason": key, "label": label, "count": blocked_by_reason.get(key, 0)}
        for key, (label, _hint) in NO_CLICK_LABELS.items()
    ]
    legacy = blocked_by_reason.get("", 0)
    if legacy:
        no_click.append({"reason": "legacy", "label": "历史未分类", "count": legacy})
    # 点击明细要不要把回收站里那些也算进来？事件的归属链接已经软删了，
    # 但它们是真实发生过的点击。「运行状态」报的是库里有几行明细，
    # 所以这里保留全量口径，只把几个面向「还活着的链接」的数字剔除回收站。
    total_events = db.execute(select(func.count()).select_from(ClickEvent)).scalar_one() or 0
    # 今日按本地自然日算，跟用户看到的日期一致。
    # 注意不能自己 combine(tzinfo=utc)：那切的是本地早上 8 点，
    # 早班那几个小时的点击会被算到昨天去。
    today_start = local_day_start(today)
    clicks_today = (
        db.execute(
            select(func.count())
            .select_from(ClickEvent)
            .where(ClickEvent.ts >= today_start, ClickEvent.blocked.is_(False))
        ).scalar_one()
        or 0
    )

    # 回收站里的不排进来：已经删掉的链接霸着 Top 榜，本身就是个 bug
    top_links = [
        {"code": code, "target": target, "clicks": int(clicks or 0)}
        for code, target, clicks in db.execute(
            select(Link.code, Link.target, Link.clicks)
            .where(Link.deleted_at.is_(None))
            .order_by(Link.clicks.desc())
            .limit(8)
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
            "platform_color": social.color(platform_key),
            "platform_icon": social.icon(platform_key),
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

    # 近 14 天按本地日期分桶，和统计页保持同一口径。
    # 窗口同样按本地自然日切，否则最后一根柱子（今天）会是空的。
    # 与统计页同一口径：先按 UTC 解释再转本地，别用裸的 "localtime"
    local_day = local_day_sql(ClickEvent.ts)
    daily_rows = (
        db.execute(
            select(local_day, func.count(ClickEvent.id))
            .where(ClickEvent.ts >= local_day_start(today - dt.timedelta(days=13)), ClickEvent.blocked.is_(False))
            .group_by(local_day)
            .order_by(local_day)
        )
        .all()
    )
    buckets: dict[str, int] = {}
    for i in range(14):
        buckets[(today - dt.timedelta(days=13 - i)).isoformat()] = 0
    for d, c in daily_rows:
        key = d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]
        buckets[key] = int(c)

    return DashboardOut(
        total_links=int(total_links),
        total_clicks=int(total_clicks),
        trash_links=trashed,
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
        no_click=no_click,
        clicks_today=int(clicks_today),
        total_events=int(total_events),
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
    require_settings_admin(request)
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
        site_tagline=str(values.get("site_tagline") or ""),
        social_guard=bool(values.get("social_guard", True)),
        social_block_hint=str(values.get("social_block_hint") or ""),
        social_tag_qq=bool(values.get("social_tag_qq", True)),
        social_tag_wechat=bool(values.get("social_tag_wechat", True)),
        notify_enabled=bool(values.get("notify_enabled", False)),
        notify_cooldown=int(values.get("notify_cooldown") or 0),
    )


@router.post("/settings", summary="保存设置")
def api_save_settings(payload: SettingsUpdate, request: Request, db: Session = Depends(get_db)):
    require_settings_admin(request)
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        if key not in EDITABLE:
            continue
        if key in {"redirect_rate_limit", "click_daily_limit", "create_daily_limit", "notify_cooldown"}:
            value = max(0, int(value))
        if key == "code_length":
            value = max(3, min(16, int(value)))
        if key == "access_mode":
            value = access.normalize_mode(value)
        set_value(db, key, value)
    policy.invalidate()
    report.record(db, "settings", actor=require_settings_admin(request), detail=", ".join(sorted(data.keys())), request=request)
    db.commit()
    return {"ok": True, "message": "已保存"}


@router.post("/settings/reset", summary="恢复默认设置")
def api_reset_settings(request: Request, db: Session = Depends(get_db)):
    require_settings_admin(request)
    for key in EDITABLE:
        row = db.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
        if row is not None:
            db.delete(row)
    policy.invalidate()
    report.record(db, "settings", actor=require_settings_admin(request), detail="恢复默认设置", request=request)
    db.commit()
    return {"ok": True, "message": "已恢复环境变量默认值"}


@router.get("/credentials", summary="当前管理员账号（不含密码）")
def api_get_credentials(request: Request):
    require_settings_admin(request)
    return {
        "ok": True,
        "username": settings.admin_username,
        "from_token": not bool(settings.admin_password),
        "env_file": str(admin_env_file_path()),
    }


@router.post("/credentials", summary="修改管理员账号密码")
def api_save_credentials(payload: dict, request: Request, db: Session = Depends(get_db)):
    """改完立刻生效，并尽力写回 .env，重启后不会退回旧密码。

    要改密码先证明自己是当前管理员：会话也可能是别人在公用电脑上留下的。
    密码换掉以后旧的 ADMIN_TOKEN 一并作废——留着等于多一把没人记得的钥匙。
    """
    actor = require_settings_admin(request)
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    current = str(payload.get("current_password") or "")

    if not check_credentials(actor, current):
        raise ServiceError("当前密码不正确", 403)
    if username and not (3 <= len(username) <= 32):
        raise ServiceError("账号需 3~32 位", 400)
    if username and any(c.isspace() for c in username):
        raise ServiceError("账号不能包含空格", 400)
    if len(password) < 8:
        raise ServiceError("新密码至少 8 位", 400)
    if len(password) > 128:
        raise ServiceError("新密码最多 128 位", 400)

    new_user = username or settings.admin_username
    settings.admin_username = new_user
    settings.admin_password = password
    settings.admin_token = ""

    persisted = write_env_vars(
        {"ADMIN_USERNAME": new_user, "ADMIN_PASSWORD": password, "ADMIN_TOKEN": ""}
    )
    report.record(db, "credentials", actor=actor, detail="修改管理员账号密码", request=request)
    db.commit()
    return {
        "ok": True,
        "username": new_user,
        "persisted": persisted,
        "message": "已保存并生效" if persisted else "已生效，但 .env 不可写，重启后需重新配置",
    }


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
            {
                "key": key,
                "label": name,
                "strategy": rules.get(key, social.DEFAULT_STRATEGY),
                "color": social.color(key),
                "icon": social.icon(key),
            }
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
    reason: str | None = Query(None, description="ua / social / social_silent / quota"),
    db: Session = Depends(get_db),
):
    """拦截留痕：原因、平台、时间、UA 摘要，不含 IP 明文（只存哈希也一并省略）。"""
    require_admin(request)
    stmt = (
        select(Link.code, ClickEvent.platform, ClickEvent.ua, ClickEvent.ts, ClickEvent.block_reason)
        .join(Link, Link.id == ClickEvent.link_id)
        .where(ClickEvent.blocked.is_(True))
        .order_by(ClickEvent.ts.desc())
        .limit(limit)
    )
    if platform and social.known(platform):
        stmt = stmt.where(ClickEvent.platform == platform)
    if reason:
        # 按原因筛：查出「配额用尽」那几条，才知道该调阈值还是该封人
        stmt = stmt.where(ClickEvent.block_reason == reason)
    items = [
        {
            "code": code,
            "platform": platform_key or "",
            "label": social.label(platform_key),
            "color": social.color(platform_key),
            "icon": social.icon(platform_key),
            "ua": (ua or "")[:90],
            "reason": block_reason or "",
            "reason_label": NO_CLICK_LABELS.get(block_reason, ("历史未分类", ""))[0]
            if block_reason
            else "历史未分类",
            "ts": ensure_iso(ts),
        }
        for code, platform_key, ua, ts, block_reason in db.execute(stmt).all()
    ]
    return {"items": items, "total": len(items)}


@router.get("/system", summary="系统信息：版本 / 运行时长 / 内存 / 日志尾巴")
def api_system(request: Request, db: Session = Depends(get_db), lines: int = Query(0, ge=0, le=200)):
    """排障时最先要看的东西，一次给全。

    日志默认不带（可能是几百 KB 的文本），前端点「看日志」再要，
    免得每次刷新面板都把整段日志重新传一遍。
    """
    require_admin(request)
    from app.core import health

    data = health.snapshot(db)
    data["log_tail"] = health.log_tail(lines=lines or 40) if lines else []
    return data


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
    require_settings_admin(request)
    rows = db.execute(select(ApiKey).order_by(ApiKey.id.desc())).scalars().all()
    return [ApiKeyOut(**row.to_dict()) for row in rows]


@router.post("/api-keys", response_model=ApiKeyOut, status_code=201, summary="新建 API 密钥")
def api_create_key(payload: ApiKeyCreate, request: Request, db: Session = Depends(get_db)):
    require_settings_admin(request)
    key = ApiKey(name=payload.name or "默认密钥", key="sk-" + secrets.token_urlsafe(24))
    db.add(key)
    db.commit()
    db.refresh(key)
    return ApiKeyOut(**key.to_dict())


@router.delete("/api-keys/{key_id}", summary="删除 API 密钥")
def api_delete_key(key_id: int, request: Request, db: Session = Depends(get_db)):
    require_settings_admin(request)
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


# ---------- 通知通道 ----------
#
# 告警一直只写在页面上：部署自检、配额告警、到期提醒都得有人主动点开
# 「运行状态」才看得见。真出事那天，通常谁都没在看。


@router.get("/notify", summary="通知通道配置与最近结果")
def api_notify_get(request: Request, db: Session = Depends(get_db)):
    require_settings_admin(request)
    from app.core import notify

    return {
        "enabled": notify_enabled(db),
        "channels": notify.state(db),
        "sources": notify_sources(db),
        "cooldown": settings.notify_cooldown,
    }


@router.post("/notify", summary="保存通知通道配置")
def api_notify_save(payload: dict, request: Request, db: Session = Depends(get_db)):
    """按键保存：空串表示清空（而不是「没填就保留旧值」）。

    密码 / 密钥类字段留空视为不改——回显是脱敏的，前端不可能把原值带回来。
    """
    require_settings_admin(request)
    from app.core import notify

    channel = str(payload.get("channel") or "").strip()
    if channel not in notify.CHANNELS:
        raise ServiceError("未知的通知通道", 422)
    values = dict(payload.get("options") or {})
    if payload.get("enabled") is not None:
        from app.core.settings_store import set_value

        set_value(db, "notify_enabled", bool(payload.get("enabled")))
    current = notify.options(db, channel)
    for secret_name in ("password", "secret"):
        if secret_name in values and not str(values[secret_name] or "").strip():
            values.pop(secret_name)
            if current.get(secret_name):
                values[secret_name] = current[secret_name]
    notify.save_options(db, channel, values)
    report.record(db, "settings", actor=require_settings_admin(request), detail=f"通知通道 {channel}", request=request)
    db.commit()
    return api_notify_get(request, db)


@router.post("/notify/test", summary="发一条测试消息")
def api_notify_test(payload: dict, request: Request, db: Session = Depends(get_db)):
    require_settings_admin(request)
    from app.core import notify

    channel = str(payload.get("channel") or "").strip()
    if channel not in notify.CHANNELS:
        raise ServiceError("未知的通知通道", 422)
    result = notify.send(
        db,
        channel,
        f"{settings.site_name} 测试消息",
        "收到这条说明通知通道是通的。\n时间：" + dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        level="info",
    )
    report.record(db, "settings", actor=require_settings_admin(request), detail=f"通知测试 {channel}", request=request)
    db.commit()
    return {**result, "message": result["message"]}


@router.post("/notify/send-digest", summary="立即推送一次告警摘要")
def api_notify_digest(request: Request, db: Session = Depends(get_db)):
    require_settings_admin(request)
    from app.core import notify

    warnings = runtime_status(db)["warnings"]
    results = notify.broadcast(db, f"{settings.site_name} 运行摘要", notify.digest_body(warnings, db), level="info")
    db.commit()
    if not results:
        raise ServiceError("还没有任何通道配置完整，先在「通知」里填一个", 422)
    sent = sum(1 for r in results if r["ok"])
    return {"ok": sent > 0, "message": f"{sent}/{len(results)} 个通道投递成功", "results": results}


def notify_enabled(db: Session) -> bool:
    """通知总开关：后台 > 环境变量。"""
    from app.core.settings_store import effective

    return bool(effective(db, "notify_enabled"))


def notify_sources(db: Session) -> list[dict]:
    """哪些事会触发通知。现在只有告警，将来加投放统计也在这里登记。"""
    return [
        {"key": "warnings", "label": "部署自检与配额告警", "desc": "运行状态里亮黄灯的那些，出现时推一条"},
        {"key": "digest", "label": "手动摘要", "desc": "随时把当前告警打包发一次"},
    ]


@router.post("/notify/expiring", summary="推送即将到期的链接提醒")
def api_notify_expiring(request: Request, days: int = Query(7, ge=1, le=30), db: Session = Depends(get_db)):
    """把「N 天内到期」的链接打包推给所有配好的通道。

    到期提醒以前只在仪表盘上摆着，得有人点开才看得见——而链接失效这件事，
    恰恰是没人看的时候发生的。这里给一个手动出口，也能被 cron 直接调。
    """
    require_settings_admin(request)
    from app.core import notify
    from app.core.timeutil import fmt_local
    from app.services import expiring_links_brief

    rows = expiring_links_brief(db, days=days, limit=50)
    if not rows:
        return {"ok": True, "count": 0, "message": f"最近 {days} 天没有要到期的链接"}
    lines = [f"站点：{settings.site_name}", "", f"以下 {len(rows)} 条将在 {days} 天内到期：", ""]
    for row in rows:
        when = fmt_local(row["expires_at"], "%m-%d %H:%M") if row.get("expires_at") else "—"
        lines.append(f"· /{row['code']}  {when}  剩余 {row['days_left']} 天  {row['target'][:60]}")
    lines += ["", "去后台「链接管理」勾选后可以批量续期：/admin"]
    body = "\n".join(lines)
    results = notify.broadcast(db, f"{settings.site_name}：{len(rows)} 条链接即将到期", body, level="warn")
    db.commit()
    if not results:
        raise ServiceError("还没有任何通道配置完整，先在「通知」里填一个", 422)
    sent = sum(1 for r in results if r["ok"])
    return {
        "ok": sent > 0,
        "count": len(rows),
        "message": f"{len(rows)} 条即将到期，{sent}/{len(results)} 个通道投递成功",
        "results": results,
    }


@router.get("/trash", summary="回收站（后台）")
def api_admin_trash(request: Request, q: str | None = Query(None), db: Session = Depends(get_db)):
    require_admin(request)
    from app.core.timeutil import ensure_aware, utcnow
    from app.services import TRASH_RETENTION_DAYS, list_deleted

    items = []
    for link in list_deleted(db, q=q):
        row = link.to_dict()
        deleted = ensure_aware(link.deleted_at)
        row["days_left"] = max(0, TRASH_RETENTION_DAYS - (utcnow() - deleted).days) if deleted else 0
        items.append(row)
    return {"items": items, "retention_days": TRASH_RETENTION_DAYS, "total": len(items)}


@router.get("/backup", summary="下载数据库备份")
def api_backup(request: Request, download: int = 1, db: Session = Depends(get_db)):
    """把库里的一切打包下载回去。

    数据能自托管，前提是数据拿得走。导出 JSON / CSV 只覆盖链接，账号、
    配置、点击明细还在库里；真要迁移或留底，得有一个「把整个库拿走」的口子。

    **不能直接 `FileResponse` 那个库文件**：站点开着 WAL，最近的写入先落在
    `-wal` 里，主库可能是几 MB 之前的版本。这里先让 sqlite 自己折成一份
    自包含的单文件（`VACUUM INTO`），再原样发回去 —— 折完的那份是真正的
    SQLite 库，`sqlite3` 打开就能用，不需要在旁边放 WAL。

    `download=0` 只返回一次同源的快照摘要（条数 / 体积），给脚本用。
    只支持 SQLite：Postgres 请用 pg_dump。
    """
    from pathlib import Path
    from tempfile import mkstemp

    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    from app import __version__
    from app.core.archive import ArchiveError, snapshot, sqlite_path

    actor = require_settings_admin(request)
    path = sqlite_path(settings.database_url)
    if path is None:
        raise ServiceError("当前不是文件型 SQLite，请用 pg_dump 备份；这里只支持下载 SQLite 库", 422)
    if not path.is_file():
        raise ServiceError(f"库文件不存在：{path.name}（内存库无法备份）", 422)

    # 折库落在自己的临时目录里，再改名到最终位置：snapshot() 的产出名是固定的
    # simpleurl.db，两个管理员同时点下载时不该抢同一个文件。
    work = Path(tempfile.mkdtemp(prefix="su-snap-"))
    fd, tmp = mkstemp(prefix="simpleurl-", suffix=".db")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        snap = snapshot(path, work)
        os.replace(snap, tmp_path)
    except (ArchiveError, OSError) as exc:
        tmp_path.unlink(missing_ok=True)
        raise ServiceError(f"备份失败：{exc}", 500) from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)

    size = tmp_path.stat().st_size
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    report.record(
        db, "settings", actor=actor,
        detail=f"下载数据库备份（{size // 1024 or 1} KB）", request=request,
    )
    db.commit()
    if download == 0:
        tmp_path.unlink(missing_ok=True)
        return {"ok": True, "size": size, "filename": f"simpleurl-{stamp}.db", "version": __version__}
    return FileResponse(
        tmp_path,
        media_type="application/octet-stream",
        filename=f"simpleurl-{stamp}.db",
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(lambda: tmp_path.unlink(missing_ok=True)),
    )


@router.post("/restore", summary="从备份包还原数据库")
async def api_restore(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """把另一个站点的备份恢复到这里。

    备份是「把全部拿走」的那一头，这是它的另一头 —— 没有它的备份只能叫导出一份文件。
    以前还原只能用 `simpleurl restore`，那要能 ssh 上去、还得找到安装目录；
    这个入口给的是「换服务器 / 搬库」时最缺的那一步。

    接受两种东西：`simpleurl backup` 打出来的 .tar.gz（里面是折好的库 + .env），
    以及后台下载的那份 .db。**先验后换**：库要能过 `PRAGMA integrity_check`、
    links 表要在，验过了才动现有数据 —— 一个坏包不能把在跑的站点换没了。
    换之前把现有库拷一份到 `.restore-backup-<时间戳>.db`，屏幕上说清楚路径。
    """
    import shutil
    import tempfile
    from pathlib import Path

    from app import __version__
    from app.core.archive import ArchiveError, extract_db, sqlite_path, verify

    actor = require_settings_admin(request)
    # 先把要回的话都算好（actor 已经拿到），后面就只剩文件操作：
    # 数据一换，任何还想读旧库的动作都会踩到「database is locked」。
    target = sqlite_path(settings.database_url)
    if target is None:
        raise ServiceError("当前不是文件型 SQLite，无法从备份包还原；Postgres 请用 pg_restore", 422)
    if not target.is_file():
        raise ServiceError(f"库文件不存在：{target.name}，还没初始化过", 422)

    raw = await file.read()
    if not raw:
        raise ServiceError("上传的文件是空的", 422)
    if len(raw) > MAX_RESTORE_BYTES:
        raise ServiceError(f"备份包太大（上限 {MAX_RESTORE_BYTES // (1024 * 1024)} MB）", 413)

    work = Path(tempfile.mkdtemp(prefix="su-restore-"))
    try:
        upload = work / "upload.bin"
        upload.write_bytes(raw)
        note = ""
        db_file = upload
        env_file = None
        if raw[:2] == b"\x1f\x8b":
            try:
                db_file, env_file, note = extract_db(upload, work / "x")
            except ArchiveError as exc:
                raise ServiceError(str(exc), 422) from exc
        else:
            moved = work / "snapshot.db"
            upload.replace(moved)
            db_file = moved
        if db_file.read_bytes()[:16] != b"SQLite format 3\x00":
            raise ServiceError("这不是一份 SQLite 备份（tar.gz 或 .db 都不是）", 422)
        try:
            links = verify(db_file)
        except ArchiveError as exc:
            raise ServiceError(str(exc), 422) from exc

        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        keep = target.with_name(f".restore-backup-{stamp}.db")
        # 先落地再换名：直接写目标文件的话，中途失败会留下一个半份库
        staging = target.with_name(f".restore-incoming-{stamp}.db")
        shutil.copyfile(db_file, staging)
        shutil.copyfile(target, keep)
        db.close()
        get_engine().dispose()
        os.replace(staging, target)
        # 旧的 WAL / SHM 属于旧库，留着会和刚换上的库对不上号
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        # 留痕只记一条，记在**换完之后**：原 session 还捏着旧库的连接，
        # 换文件之后再用它写就是 `database is locked`（先把错误抛出去，
        # 屏幕上只剩 Internal Server Error，而还原其实已经成功了）。
        with session_scope() as fresh:
            report.record(
                fresh, "settings", actor=actor,
                detail=f"从备份还原（{links} 条链接，原库留底 {keep.name}）", request=request,
            )
        return {
            "ok": True,
            "links": links,
            "kept": str(keep),
            "version": __version__,
            "note": note,
            "env_ignored": env_file is not None,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


@router.post("/import/csv", summary="从 CSV 清单导入")
def api_import_csv(payload: dict, request: Request, db: Session = Depends(get_db)):
    """导入一张表格就建一批短链。

    表头认中文也认英文，认不出来就按「哪一列像链接」猜；
    短码冲突自动换一个，不会覆盖已有链接。
    """
    require_admin(request)
    from app.services import import_links_csv

    text = str(payload.get("payload") or "")
    if not text.strip():
        raise ServiceError("粘贴内容为空", 422)
    result = import_links_csv(db, text)
    report.record(
        db,
        "import",
        actor=require_admin(request),
        detail=f"CSV 导入 {result['imported']} 条，换码 {result['renamed']} 条，失败 {result['failed']} 条",
        request=request,
    )
    db.commit()
    return result
