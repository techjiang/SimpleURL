"""业务逻辑层：创建、查询、更新、删除、统计。"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core import guard, policy, social
from app.core.codes import generate_unique_code, normalize_code, validate_custom_code
from app.core.config import settings
from app.core.clicks import click_buffer
from app.core.security import is_public_url, ip_hash, parse_user_agent, rate_limiter
from app.core.settings_store import code_length_value, effective
from app.core.titles import guess_title
from app.core.timeutil import ensure_aware, utcnow
from app.models import ClickEvent, Link
from app.schemas import LinkCreate, LinkUpdate

logger = logging.getLogger("simpleurl")

EXPIRING_DAYS = 7

EXPORT_HEADER = ["短码", "短链接", "目标地址", "标题", "标签", "点击", "状态", "过期时间", "创建时间"]


class ServiceError(Exception):
    """业务错误，code 为 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _ensure_target(url: str) -> str:
    if not is_public_url(url):
        raise ServiceError("目标地址不合法：仅支持 http/https 且可解析的公开地址", 422)
    return url


def _expiry_guard(value: dt.datetime | None) -> dt.datetime | None:
    value = ensure_aware(value)
    if value is None:
        return None
    if value <= utcnow():
        raise ServiceError("过期时间必须晚于当前时间", 422)
    return value


def check_create_quota(db: Session, ip: str | None) -> None:
    """单 IP 每日创建上限。

    额度走后台可改的 `create_daily_limit`（原来只认环境变量，导致
    「设置 → 每日创建上限」改了也不生效），并且按自然日重置——滚动 24 小时
    会让昨晚刷满的人今早继续被拦。
    """
    if not ip:
        return
    limit = int(effective(db, "create_daily_limit") or 0)
    if limit <= 0:
        return
    if not rate_limiter.allow(f"create:{ip}", limit, 86400, daily=True):
        raise ServiceError(f"今日创建次数已达上限（{limit} 次），请明天再试", 429)


def create_link(db: Session, payload: LinkCreate, ip: str | None = None) -> Link:
    target = _ensure_target(payload.target)
    expires_at = _expiry_guard(payload.expires_at)
    check_create_quota(db, ip)

    custom = normalize_code(payload.code) if payload.code else None
    if custom:
        ok, msg = validate_custom_code(custom, settings.code_min_length, settings.code_max_length)
        if not ok:
            raise ServiceError(msg, 422)
        exists = db.execute(select(Link.id).where(func.lower(Link.code) == custom.lower())).first()
        if exists:
            raise ServiceError("该短码已被占用，换一个试试", 409)

    code = custom or generate_unique_code(code_length_value(db), settings.code_no_ambiguous)
    link = Link(
        code=code,
        target=target,
        title=payload.title,
        note=payload.note,
        tags=json.dumps(payload.tags, ensure_ascii=False) if payload.tags else None,
        expires_at=expires_at,
    )
    db.add(link)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ServiceError("该短码已被占用，换一个试试", 409) from exc
    if not link.title and effective(db, "fetch_title"):
        # 拿不到就留空，绝不让标题获取拖垮创建
        link.title = guess_title(link.target)
    logger.info("创建短链 %s -> %s", link.code, link.target)
    return link


def get_link_by_code(db: Session, code: str) -> Link:
    code = normalize_code(code)
    if not code:
        raise ServiceError("短码不能为空", 404)
    link = db.execute(select(Link).where(func.lower(Link.code) == code.lower())).scalar_one_or_none()
    if link is None:
        raise ServiceError("短链接不存在", 404)
    return link


def _base_query(
    q: str | None = None,
    tag: str | None = None,
    status: str | None = None,
    sort: str = "created_desc",
) -> select:
    stmt = select(Link)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(Link.code.ilike(like), Link.target.ilike(like), (Link.title or "").ilike(like), Link.tags.ilike(like))
        )
    if tag:
        stmt = stmt.where(Link.tags.ilike(f'%"{tag.strip()}"%'))
    if status == "enabled":
        stmt = stmt.where(Link.enabled.is_(True))
    elif status == "disabled":
        stmt = stmt.where(Link.enabled.is_(False))
    elif status == "expired":
        stmt = stmt.where(Link.expires_at.is_not(None), Link.expires_at <= utcnow())
    elif status == "expiring":
        # 7 天内到期：到期前没人提醒，链接就静静死掉了
        horizon = utcnow() + dt.timedelta(days=EXPIRING_DAYS)
        stmt = stmt.where(Link.expires_at.is_not(None), Link.expires_at > utcnow(), Link.expires_at <= horizon)

    sort_map = {
        "created_desc": Link.created_at.desc(),
        "created_asc": Link.created_at.asc(),
        "clicks_desc": Link.clicks.desc(),
        "clicks_asc": Link.clicks.asc(),
        "code_asc": Link.code.asc(),
    }
    return stmt.order_by(sort_map.get(sort, Link.created_at.desc()))


def list_links(
    db: Session,
    q: str | None = None,
    tag: str | None = None,
    status: str | None = None,
    sort: str = "created_desc",
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[Link]]:
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    stmt = _base_query(q=q, tag=tag, status=status, sort=sort)

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    items = list(
        db.execute(
            stmt.offset((page - 1) * page_size)
            .limit(page_size)
            .options(selectinload(Link.events))  # to_dict 不碰 events，这里是为了避免后续按需加载
        )
        .scalars()
        .all()
    )
    return total, items


def all_links_for_export(
    db: Session,
    q: str | None = None,
    tag: str | None = None,
    status: str | None = None,
    sort: str = "created_desc",
) -> list[Link]:
    """导出用：不带 events 懒加载，最多取 export_max_rows 条。"""
    stmt = _base_query(q=q, tag=tag, status=status, sort=sort).limit(settings.export_max_rows)
    return list(db.execute(stmt).scalars().all())


def update_link(db: Session, code: str, payload: LinkUpdate) -> Link:
    link = get_link_by_code(db, code)
    data = payload.model_dump(exclude_unset=True)

    if "target" in data and data["target"] is not None:
        data["target"] = _ensure_target(data["target"])
    if "expires_at" in data:
        data["expires_at"] = _expiry_guard(data["expires_at"])
    if "tags" in data:
        data["tags"] = json.dumps(data["tags"], ensure_ascii=False) if data["tags"] else None
    data.pop("code", None)

    for key, value in data.items():
        setattr(link, key, value)
    db.flush()
    logger.info("更新短链 %s", link.code)
    return link


def delete_link(db: Session, code: str) -> None:
    link = get_link_by_code(db, code)
    db.delete(link)
    db.flush()
    logger.info("删除短链 %s", link.code)


BULK_ACTIONS = ("enable", "disable", "delete")


def bulk_update(db: Session, codes: list[str], action: str) -> dict:
    """批量启用 / 停用 / 删除。不存在的短码跳过，不让一条脏码毁掉整批。"""
    if action not in BULK_ACTIONS:
        raise ServiceError("不支持的操作", 422)
    wanted = [normalize_code(c) for c in codes if normalize_code(c)]
    if not wanted:
        raise ServiceError("没有要处理的短码", 422)
    if len(wanted) > 500:
        raise ServiceError("一次最多处理 500 条", 422)

    rows = list(
        db.execute(select(Link).where(func.lower(Link.code).in_([c.lower() for c in wanted]))).scalars().all()
    )
    found = {row.code.lower() for row in rows}
    affected, missing = 0, [c for c in wanted if c.lower() not in found]
    for row in rows:
        if action == "delete":
            db.delete(row)
        else:
            row.enabled = action == "enable"
        affected += 1
    db.flush()
    logger.info("批量 %s：%d 条", action, affected)
    return {"ok": True, "action": action, "affected": affected, "missing": missing}


def resolve_redirect(db: Session, code: str) -> tuple[Link, str]:
    """返回 (链接, 目标地址)。已停用/过期/无效都抛错。"""
    link = get_link_by_code(db, code)
    if not link.enabled:
        raise ServiceError("该短链接已停用", 410)
    if link.is_expired:
        raise ServiceError("该短链接已过期", 410)
    if not is_public_url(link.target):
        raise ServiceError("目标地址已失效", 410)
    return link, link.target


def record_click(
    db: Session,
    link: Link,
    referrer: str | None,
    ua: str | None,
    ip: str | None,
    blocked: bool = False,
    snapshot: dict | None = None,
) -> dict:
    """记录一次点击。

    走延迟计数：只入内存缓冲，不直接写库。几种「不计数」的情况都留痕
    （打上 blocked 标记），后台才看得到拦了多少，但它们都不会进点击数。

    社交平台策略里的 silent 在这里生效：照常跳转、只记不算。
    返回 {'recorded': bool, 'blocked': bool, 'platform': str | None}
    """
    platform = social.detect(ua, referrer)
    snap = snapshot or policy.snapshot(db)
    if blocked:
        # 社交平台策略直接拦下的访问：只留痕，不算点击
        click_buffer.add(db, link.id, referrer, ua, ip, blocked=True, platform=platform)
        return {"recorded": False, "blocked": True, "platform": platform}

    blocked_rule = guard.is_ua_blocked(ua, snap.get("ua_rules") or [])
    if blocked_rule:
        click_buffer.add(db, link.id, referrer, ua, ip, blocked=True, platform=platform)
        logger.info("UA 屏蔽命中 %s -> %s", link.code, blocked_rule)
        return {"recorded": False, "blocked": True, "platform": platform}

    allowed, reason = guard.check_click_quota(db, ip, snap)
    if not allowed:
        logger.info("点击配额拦截 %s -> %s", link.code, reason)
        return {"recorded": False, "blocked": False, "reason": reason, "platform": platform}

    if platform and snap.get("social_guard") and (snap.get("platforms") or {}).get(platform) == "silent":
        # 放行不计数：怀疑被刷量的平台上，真实用户照样能打开，只是不进统计
        click_buffer.add(db, link.id, referrer, ua, ip, blocked=True, platform=platform)
        return {"recorded": False, "blocked": True, "platform": platform}

    click_buffer.add(db, link.id, referrer, ua, ip, blocked=False, platform=platform)
    return {"recorded": True, "blocked": False, "platform": platform}


def get_stats(db: Session, code: str, days: int = 30) -> dict:
    link = get_link_by_code(db, code)
    days = max(1, min(365, days))
    now = utcnow()
    since = now - dt.timedelta(days=days - 1)

    daily_rows = db.execute(
        select(func.date(ClickEvent.ts), func.count(ClickEvent.id))
        .where(ClickEvent.link_id == link.id, ClickEvent.ts >= since)
        .group_by(func.date(ClickEvent.ts))
        .order_by(func.date(ClickEvent.ts))
    ).all()

    # 补全缺失日期
    buckets: dict[str, int] = {}
    for i in range(days):
        d = (now - dt.timedelta(days=days - 1 - i)).date().isoformat()
        buckets[d] = 0
    for d, c in daily_rows:
        key = d.isoformat() if hasattr(d, "isoformat") else str(d)
        buckets[key] = int(c)

    referrer_rows = (
        db.execute(
            select(ClickEvent.referrer, func.count(ClickEvent.id))
            .where(ClickEvent.link_id == link.id, ClickEvent.referrer.is_not(None), ClickEvent.referrer != "")
            .group_by(ClickEvent.referrer)
            .order_by(func.count(ClickEvent.id).desc())
            .limit(10)
        )
        .all()
    )
    top = [
        {"source": _host(r) or "直接访问", "count": int(c)}
        for r, c in referrer_rows
        if (_host(r) is not None)
    ]
    if not top:
        top = [{"source": "直接访问", "count": link.clicks or 0}]

    device_rows = (
        db.execute(
            select(ClickEvent.device, func.count(ClickEvent.id))
            .where(ClickEvent.link_id == link.id)
            .group_by(ClickEvent.device)
            .order_by(func.count(ClickEvent.id).desc())
        )
        .all()
    )
    device_labels = {"desktop": "桌面端", "mobile": "移动端", "bot": "机器人 / 脚本", "other": "其他", None: "未知"}
    devices = [
        {"device": device_labels.get(dev, dev or "未知"), "count": int(c)}
        for dev, c in device_rows
    ]

    # 按来源平台分账：微信 / QQ 里点开多少，一屏看清
    platform_rows = (
        db.execute(
            select(ClickEvent.platform, func.count(ClickEvent.id))
            .where(ClickEvent.link_id == link.id, ClickEvent.blocked.is_(False))
            .group_by(ClickEvent.platform)
            .order_by(func.count(ClickEvent.id).desc())
        )
        .all()
    )
    platforms = [
        {"platform": social.label(key), "key": key or "other", "count": int(c)}
        for key, c in platform_rows
    ]
    blocked_count = db.execute(
        select(func.count(ClickEvent.id)).where(ClickEvent.link_id == link.id, ClickEvent.blocked.is_(True))
    ).scalar_one()

    unique = db.execute(
        select(func.count(func.distinct(ClickEvent.ip_hash))).where(
            ClickEvent.link_id == link.id,
            ClickEvent.ip_hash.is_not(None),
            ClickEvent.blocked.is_(False),
        )
    ).scalar_one()

    last_click = db.execute(
        select(func.max(ClickEvent.ts)).where(ClickEvent.link_id == link.id)
    ).scalar_one_or_none()

    return {
        "code": link.code,
        "clicks": link.clicks or 0,
        "unique_visitors": int(unique or 0),
        "created_at": link.created_at,
        "last_click_at": last_click,
        "daily": [{"date": k, "count": v} for k, v in buckets.items()],
        "top_referrers": top,
        "devices": devices,
        "platforms": platforms,
        "blocked": int(blocked_count or 0),
    }


def links_to_csv(links: list[Link]) -> str:
    """把链接列表导成 CSV 文本。"""

    def _tags(link: Link) -> str:
        if not link.tags:
            return ""
        try:
            return ", ".join(json.loads(link.tags))
        except (ValueError, TypeError):
            return ""

    def _status(link: Link) -> str:
        if link.is_expired:
            return "已过期"
        return "已启用" if link.enabled else "已停用"

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_HEADER)
    for link in links:
        created = ensure_aware(link.created_at)
        expires = ensure_aware(link.expires_at)
        writer.writerow(
            [
                link.code,
                link.code,
                link.target,
                link.title or "",
                _tags(link),
                link.clicks or 0,
                _status(link),
                expires.strftime("%Y-%m-%d %H:%M") if expires else "",
                created.strftime("%Y-%m-%d %H:%M") if created else "",
            ]
        )
    return buf.getvalue()


def _host(referrer: str) -> str | None:
    if not referrer:
        return None
    from urllib.parse import urlparse

    try:
        h = urlparse(referrer).hostname
    except ValueError:
        return None
    return h or None


# ---------- 运行状态 / 自动清理 ----------

# 点击事件留多久。统计只看 30 天趋势，留一年已经很有余量；
# 无上限增长的单表迟早把 SQLite 拖垮。可用 CLICK_EVENT_RETENTION_DAYS 覆盖。
CLICK_EVENT_RETENTION_DAYS = settings.retention_days or 365


def runtime_status(db: Session) -> dict:
    """运行状态：排队多少、丢了什么、库有多大、哪些链接快到期。

    「服务健康吗」不该只回答 200。写库卡住时点击数会堆在内存里、
    日志表会无限长，这两件事都是静默发生的。
    """
    from sqlalchemy import text

    from app.core.access import warnings as access_warnings
    from app.core.clicks import click_buffer

    link_count = int(db.execute(select(func.count()).select_from(Link)).scalar_one() or 0)
    event_count = int(db.execute(select(func.count()).select_from(ClickEvent)).scalar_one() or 0)
    db_size = None
    try:
        if settings.database_url.startswith("sqlite"):
            # page_count * page_size 就是整库字节数，不用去碰文件系统
            page_count = db.execute(text("PRAGMA page_count")).scalar_one()
            page_size = db.execute(text("PRAGMA page_size")).scalar_one()
            db_size = int(page_count or 0) * int(page_size or 0)
    except Exception:  # noqa: BLE001 - Postgres 或权限不足时不显示即可
        db_size = None

    horizon = utcnow() + dt.timedelta(days=EXPIRING_DAYS)
    expiring = int(
        db.execute(
            select(func.count())
            .select_from(Link)
            .where(Link.expires_at.is_not(None), Link.expires_at > utcnow(), Link.expires_at <= horizon)
        ).scalar_one()
        or 0
    )
    return {
        "links": link_count,
        "click_events": event_count,
        "db_size": db_size,
        "pending_clicks": click_buffer.pending(),
        "dropped_clicks": click_buffer.dropped(),
        "expiring_links": expiring,
        "retention_days": CLICK_EVENT_RETENTION_DAYS,
        "warnings": access_warnings(),
    }


def prune_click_events(db: Session, days: int = CLICK_EVENT_RETENTION_DAYS) -> int:
    """删掉过期点击明细，返回删除条数。

    只删明细，不动 links.clicks —— 累计点击数是要长期保留的口径，
    图表看一年，总数看一辈子。
    """
    cutoff = utcnow() - dt.timedelta(days=max(1, days))
    rows = db.execute(select(ClickEvent.id).where(ClickEvent.ts < cutoff).limit(5000)).scalars().all()
    if not rows:
        return 0
    db.execute(ClickEvent.__table__.delete().where(ClickEvent.id.in_(list(rows))))
    db.flush()
    logger.info("清理 %d 天前的点击明细：%d 条", days, len(rows))
    return len(rows)


# ---------- 延迟计数辅助 ----------


def pending_clicks(db: Session, code: str) -> int:
    """某条链接还没落库的点击数。"""
    link = get_link_by_code(db, code)
    return click_buffer.pending_for(link.id)


def flush_clicks() -> int:
    """立即结算缓冲里的点击，返回写入条数。"""
    return click_buffer.flush()


# ---------- YOURLS 迁移 ----------


def import_yourls(db: Session, rows: list[dict]) -> dict:
    """把 YOURLS 导出的记录灌进库。

    短码冲突时自动换一个可用的码，不让整批导入失败。
    """
    imported, skipped, failed = 0, 0, 0
    for row in rows:
        target = _ensure_target(row.get("target", ""))
        code = normalize_code(row.get("code") or "")
        if not target or not code:
            failed += 1
            continue
        ok, _msg = validate_custom_code(code, settings.code_min_length, settings.code_max_length)
        if not ok:
            code = generate_unique_code(code_length_value(db), settings.code_no_ambiguous)
        if _code_taken(db, code):
            code = generate_unique_code(code_length_value(db), settings.code_no_ambiguous)
            skipped += 1
        link = Link(
            code=code,
            target=target,
            title=(row.get("title") or "").strip()[:255] or None,
            clicks=int(row.get("clicks") or 0),
        )
        created = row.get("created_at")
        if created:
            parsed = ensure_aware(created) if isinstance(created, dt.datetime) else _parse_iso(created)
            if parsed:
                link.created_at = parsed
        db.add(link)
        try:
            db.flush()
            imported += 1
        except IntegrityError:
            db.rollback()
            failed += 1
    logger.info("YOURLS 迁移完成：导入 %d，换码 %d，失败 %d", imported, skipped, failed)
    return {"imported": imported, "renamed": skipped, "failed": failed, "total": len(rows)}


def _code_taken(db: Session, code: str) -> bool:
    return db.execute(select(Link.id).where(func.lower(Link.code) == code.lower())).first() is not None


def _parse_iso(value: str):
    try:
        return ensure_aware(dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None
