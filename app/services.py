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

from app.core import guard, social
from app.core.codes import generate_unique_code, normalize_code, validate_custom_code
from app.core.config import settings
from app.core.clicks import click_buffer
from app.core.security import is_public_url, ip_hash, parse_user_agent, rate_limiter
from app.core.settings_store import effective
from app.core.titles import guess_title
from app.core.timeutil import ensure_aware, utcnow
from app.models import ClickEvent, Link
from app.schemas import LinkCreate, LinkUpdate

logger = logging.getLogger("simpleurl")

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


def check_create_quota(ip: str | None) -> None:
    if not ip or settings.create_daily_limit <= 0:
        return
    if not rate_limiter.allow(f"create:{ip}", settings.create_daily_limit, 86400):
        raise ServiceError("今日创建次数已达上限，请明天再试", 429)


def create_link(db: Session, payload: LinkCreate, ip: str | None = None) -> Link:
    target = _ensure_target(payload.target)
    expires_at = _expiry_guard(payload.expires_at)
    check_create_quota(ip)

    custom = normalize_code(payload.code) if payload.code else None
    if custom:
        ok, msg = validate_custom_code(custom, settings.code_min_length, settings.code_max_length)
        if not ok:
            raise ServiceError(msg, 422)
        exists = db.execute(select(Link.id).where(func.lower(Link.code) == custom.lower())).first()
        if exists:
            raise ServiceError("该短码已被占用，换一个试试", 409)

    code = custom or generate_unique_code(settings.code_length)
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
) -> dict:
    """记录一次点击。

    走延迟计数：只入内存缓冲，不直接写库。UA 命中屏蔽规则时也照样记一条
    （打上 blocked 标记），方便后台看到拦了多少，但不会算进点击数。
    微信 / QQ 等平台的访问会打上来源标记，方便统计里按平台分账。
    返回 {'recorded': bool, 'blocked': bool, 'platform': str | None}
    """
    platform = social.detect(ua, referrer)
    if blocked:
        # 社交平台策略直接拦下的访问：只留痕，不算点击
        click_buffer.add(db, link.id, referrer, ua, ip, blocked=True, platform=platform)
        return {"recorded": False, "blocked": True, "platform": platform}

    blocked_rule = guard.is_ua_blocked(ua, guard.blocked_ua_rules(db))
    if blocked_rule:
        click_buffer.add(db, link.id, referrer, ua, ip, blocked=True, platform=platform)
        logger.info("UA 屏蔽命中 %s -> %s", link.code, blocked_rule)
        return {"recorded": False, "blocked": True, "platform": platform}

    allowed, reason = guard.check_click_quota(db, ip)
    if not allowed:
        logger.info("点击配额拦截 %s -> %s", link.code, reason)
        return {"recorded": False, "blocked": False, "reason": reason, "platform": platform}

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
            code = generate_unique_code(settings.code_length)
        if _code_taken(db, code):
            code = generate_unique_code(settings.code_length)
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
