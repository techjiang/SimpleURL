"""业务逻辑层：创建、查询、更新、删除、统计。"""

from __future__ import annotations

import datetime as dt
import json
import logging

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.codes import generate_unique_code, normalize_code, validate_custom_code
from app.core.config import settings
from app.core.security import is_public_url, ip_hash, rate_limiter
from app.core.timeutil import ensure_aware, utcnow
from app.models import ClickEvent, Link
from app.schemas import LinkCreate, LinkUpdate

logger = logging.getLogger("simpleurl")


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
    except IntegrityError as exc:  # noqa: F841
        db.rollback()
        raise ServiceError("该短码已被占用，换一个试试", 409) from exc
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
    stmt = stmt.order_by(sort_map.get(sort, Link.created_at.desc()))

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    items = list(db.execute(stmt.offset((page - 1) * page_size).limit(page_size)).scalars().all())
    return total, items


def update_link(db: Session, code: str, payload: LinkUpdate) -> Link:
    link = get_link_by_code(db, code)
    data = payload.model_dump(exclude_unset=True)

    if "target" in data and data["target"] is not None:
        data["target"] = _ensure_target(data["target"])
    if "expires_at" in data:
        data["expires_at"] = _expiry_guard(data["expires_at"])
    if "tags" in data:
        data["tags"] = json.dumps(data["tags"], ensure_ascii=False) if data["tags"] else None
    if "code" in data:
        del data["code"]

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


def record_click(db: Session, link: Link, referrer: str | None, ua: str | None, ip: str | None) -> None:
    link.clicks = (link.clicks or 0) + 1
    db.add(
        ClickEvent(
            link_id=link.id,
            referrer=(referrer or "")[:512] or None,
            ua=(ua or "")[:512] or None,
            ip_hash=ip_hash(ip) or None,
        )
    )
    db.flush()


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

    unique = db.execute(
        select(func.count(func.distinct(ClickEvent.ip_hash))).where(
            ClickEvent.link_id == link.id, ClickEvent.ip_hash.is_not(None)
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
    }


def _host(referrer: str) -> str | None:
    if not referrer:
        return None
    from urllib.parse import urlparse

    try:
        h = urlparse(referrer).hostname
    except ValueError:
        return None
    return h or None
