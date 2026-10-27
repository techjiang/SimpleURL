"""业务逻辑层：创建、查询、更新、删除、统计。"""

from __future__ import annotations

import datetime as dt
import json
import logging

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import runtime
from app.core.clickbuffer import click_buffer
from app.core.codes import generate_unique_code, normalize_code, validate_custom_code
from app.core.config import settings
from app.core.fetcher import fetch_title
from app.core.security import is_public_url, ip_hash, rate_limiter
from app.core.db import session_scope
from app.core.timeutil import ensure_aware, utcnow
from app.models import ClickEvent, GuardEvent, Link
from app.schemas import LinkCreate, LinkUpdate

logger = logging.getLogger("simpleurl")


class ServiceError(Exception):
    """业务错误，code 为 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def check_target_policy(url: str) -> None:
    """目标域名是否符合站点的黑白名单。创建、编辑、迁移、跳转都走这里。

    统一在一个函数里判断，是为了避免「创建时拦住、编辑时能绕过去」这种只堵一半的洞。
    """
    from app.core.security import LIST_MODE_ALLOW, target_policy

    mode = LIST_MODE_ALLOW if runtime.allowed_hosts() else "off"
    reason = target_policy(url, runtime.blocked_hosts(), runtime.allowed_hosts(), mode)
    if reason:
        raise ServiceError(f"目标地址不受支持：{reason}", 422)


def _ensure_target(url: str) -> str:
    if not is_public_url(url):
        raise ServiceError("目标地址不合法：仅支持 http/https 且可解析的公开地址", 422)
    check_target_policy(url)
    return url


def _expiry_guard(value: dt.datetime | None) -> dt.datetime | None:
    value = ensure_aware(value)
    if value is None:
        return None
    if value <= utcnow():
        raise ServiceError("过期时间必须晚于当前时间", 422)
    return value


def check_create_quota(ip: str | None) -> None:
    """单 IP 每日创建配额（内存滑动窗口）。

    窗口从第一次创建算起满 24 小时，不是自然日；跨天不清零但会自然滑出。
    代价是进程重启会重置计数——创建滥用本来就是「防毛毛雨」，不值得为它上库。
    """
    limit = runtime.get_int("create_daily_limit")
    if not ip or limit <= 0:
        return
    if not rate_limiter.allow(f"create:{ip}", limit, 86400):
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

    # 自定义短码为纯数字时会被浏览器 / 手机识别成端口号，粘贴即失效
    if custom and custom.isdigit():
        raise ServiceError("纯数字短码会被当成端口号，请含至少一个字母", 422)

    code = custom or generate_unique_code(runtime.get_int("code_length"))
    title = payload.title
    if not title and runtime.get_bool("auto_title"):
        # 抓不到就用 None，让前端显示 URL 兜底，不塞一个假标题
        title = fetch_title(target)
    link = Link(
        code=code,
        target=target,
        title=title,
        note=payload.note,
        tags=json.dumps(payload.tags, ensure_ascii=False) if payload.tags else None,
        expires_at=expires_at,
        max_clicks=payload.max_clicks,
        daily_clicks_limit=payload.daily_clicks_limit,
        pass_hash=hash_pass(payload.password),
    )
    db.add(link)
    try:
        db.flush()
    except IntegrityError as exc:  # noqa: F841
        db.rollback()
        raise ServiceError("该短码已被占用，换一个试试", 409) from exc
    logger.info("创建短链 %s -> %s", link.code, link.target)
    return link


def code_available(db: Session, raw: str) -> tuple[bool, str]:
    """自定义短码可用性预检：给前端做实时校验，别等提交了才知道被占。"""
    code = normalize_code(raw)
    if not code:
        return False, "短码不能为空"
    ok, msg = validate_custom_code(code, settings.code_min_length, settings.code_max_length)
    if not ok:
        return False, msg
    exists = db.execute(select(Link.id).where(func.lower(Link.code) == code.lower())).first()
    if exists:
        return False, "该短码已被占用"
    return True, "可以用"


def _url_key(url: str) -> str:
    """归一化 URL 用于查重：忽略协议差异、末尾斜杠与 #片段。"""
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/") or "/"
    query = ("?" + parts.query) if parts.query else ""
    return urlunsplit(("", host, path, query.lstrip("?"), ""))


def find_similar(db: Session, target: str, limit: int = 5) -> list[dict]:
    """找出指向同一地址的已有短码，按点击量从高到低。"""
    from app.core.security import normalize_url

    normalized = normalize_url(target) or target.strip()
    key = _url_key(normalized)
    if not key:
        return []

    rows = list(
        db.execute(
            select(Link).where(func.lower(Link.target).contains(key.split("/", 1)[0] or key)).limit(200)
        ).scalars()
    )
    hits = [row for row in rows if _url_key(row.target) == key]
    hits.sort(key=lambda r: (not r.enabled, -(r.clicks or 0)))
    return [
        {
            "code": row.code,
            "title": row.title,
            "target": row.target,
            "clicks": int(row.clicks or 0),
            "enabled": row.enabled,
            "expired": row.is_expired,
        }
        for row in hits[:limit]
    ]


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
    data = payload.model_dump(
        exclude_unset=True,
        exclude={
            "set_password",
            "clear_password",
            "clear_expiry",
            "clear_max_clicks",
            "clear_daily_limit",
        },
    )

    if "target" in data and data["target"] is not None:
        data["target"] = _ensure_target(data["target"])
    if "expires_at" in data:
        data["expires_at"] = _expiry_guard(data["expires_at"])
    if payload.clear_expiry:
        data["expires_at"] = None
    if "tags" in data:
        data["tags"] = json.dumps(data["tags"], ensure_ascii=False) if data["tags"] else None
    data.pop("code", None)

    for key, value in data.items():
        setattr(link, key, value)

    if payload.set_password:
        link.pass_hash = hash_pass(payload.set_password)
    elif payload.clear_password:
        link.pass_hash = None
    if payload.clear_max_clicks:
        link.max_clicks = None
    if payload.clear_daily_limit:
        link.daily_clicks_limit = None
        # 额度跟着上限一起清，否则下次再设个更高的值会「继承」上一轮的已用数
        link.daily_used = 0
        link.daily_date = None

    db.flush()
    logger.info("更新短链 %s", link.code)
    return link


def hash_pass(raw: str | None) -> str | None:
    """访问口令按密钥同规格哈希，库里永远没有明文。"""
    from app.core import crypto

    if not raw:
        return None
    return crypto.hash_secret(raw)


def verify_pass(link: Link, raw: str | None) -> bool:
    from app.core import crypto

    if not link.pass_hash:
        return True
    return crypto.verify_secret((raw or "").strip(), link.pass_hash)


class LinkUnavailable(ServiceError):
    """链接不可用；附带原因代码，便于页面区分文案。"""

    def __init__(self, message: str, reason: str, status: int = 410):
        super().__init__(message, status)
        self.reason = reason


def delete_link(db: Session, code: str) -> None:
    link = get_link_by_code(db, code)
    db.delete(link)
    db.flush()
    logger.info("删除短链 %s", link.code)


def resolve_redirect(db: Session, code: str) -> tuple[Link, str]:
    """返回 (链接, 目标地址)。已停用 / 过期 / 点满 / 无效都抛 LinkUnavailable。"""
    link = get_link_by_code(db, code)
    if not link.enabled:
        raise LinkUnavailable("该短链接已停用", "disabled")
    if link.is_expired:
        raise LinkUnavailable("该短链接已过期", "expired")
    if link.exhausted:
        raise LinkUnavailable("该短链接的访问次数已用完", "exhausted")
    if link.daily_exhausted:
        raise LinkUnavailable("该短链接今日额度已用完，明天再来", "daily")
    if not is_public_url(link.target):
        raise LinkUnavailable("目标地址已失效", "invalid")
    try:
        check_target_policy(link.target)
    except ServiceError as exc:
        # 事后被拉黑的域名不该继续靠旧短码跳转，否则屏蔽名单形同虚设
        raise LinkUnavailable("该短链接已不可用", "blocked") from exc
    return link, link.target


def record_click(db: Session, link: Link, referrer: str | None, ua: str | None, ip: str | None) -> None:
    """点击先入内存缓冲，由后台线程批量落库（见 core/clickbuffer.py）。

    这里只把内存里的计数 +1，让同请求内的页面能立刻看到「刚被点过」，
    真正的持久化交给批量事务。

    每日额度走同一条队列，不单独查库扣数：额度要和点击一起原子地记账，
    否则「还剩 1 次」会被并发请求同时读到，一秒钟把额度超卖成一天。
    """
    limit = int(link.daily_clicks_limit) if link.has_daily_limit else None
    if limit:
        # 已用 = 库里落下的 + 缓冲里排队的，两者相加才是真实节奏
        used = click_buffer.daily_used(link.id, persisted=link.daily_used_today)
        if used >= limit:
            raise LinkUnavailable("该短链接今日额度已用完，明天再来", "daily")
    click_buffer.add(
        link.id,
        referrer=referrer,
        ua=ua,
        ip_hash=ip_hash(ip) or None,
        daily_limit=limit,
    )


def log_guard_event(db: Session, code: str | None, kind: str, ua: str | None,
                    ip: str | None, detail: str = "") -> None:
    """防洪留痕。写失败不影响跳转——留痕是辅助，不是主流程。"""
    try:
        db.add(
            GuardEvent(
                code=(code or "")[:32] or None,
                kind=kind[:24],
                ua=(ua or "")[:512] or None,
                ip_hash=ip_hash(ip) or None,
                detail=(detail or "")[:255] or None,
            )
        )
        db.flush()
    except Exception as exc:  # pragma: no cover
        logger.warning("写防洪日志失败：%s", exc)


def purge_old_clicks(days: int | None = None) -> int:
    """按保留期清理点击明细，只留累计点击数。

    短链服务的明细表是最容易失控的一张表：一条热链一年就能堆几十万行，
    而 30 天以外的明细除了占空间几乎没有用处。
    """
    from app import admin_service

    keep = runtime.get_int("retention_days") if days is None else days
    if keep <= 0:
        return 0
    with session_scope() as db:
        removed = admin_service.purge_clicks(db, before_days=keep)
        db.commit()
    if removed:
        logger.info("清理 %d 天前的点击明细：%d 条", keep, removed)
    return removed


def get_stats(db: Session, code: str, days: int = 30) -> dict:
    link = get_link_by_code(db, code)
    days = max(1, min(365, days))
    now = utcnow()
    since = now - dt.timedelta(days=days - 1)

    daily_rows = db.execute(
        select(func.date(ClickEvent.ts), func.count(ClickEvent.id))
        .where(ClickEvent.link_id == link.id, ClickEvent.ts >= since)
        .group_by(func.date(ClickEvent.ts))
    ).all()

    # 补全缺失日期。天从旧到新排列：直接改 dict 插入顺序就等于排好序，
    # 不用再依赖 SQL 的 date() 排序（SQLite 与 Postgres 对 date 的返回类型并不一致）。
    buckets: dict[str, int] = {}
    for i in range(days):
        d = (now - dt.timedelta(days=days - 1 - i)).date().isoformat()
        buckets[d] = 0
    for d, c in daily_rows:
        key = d.isoformat() if hasattr(d, "isoformat") else str(d)
        if key in buckets:
            buckets[key] = int(c)
        else:
            # 万一时区导致多出一天，按日期插到末尾也无害
            buckets[key] = int(c)
    buckets = dict(sorted(buckets.items()))

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

    # 今日口径按 UTC 日切，和每日额度同一把尺子，免得两个数字互相打架
    today_start = dt.datetime.combine(now.date(), dt.time.min, tzinfo=dt.timezone.utc)
    clicks_today = int(
        db.execute(
            select(func.count(ClickEvent.id)).where(
                ClickEvent.link_id == link.id, ClickEvent.ts >= today_start
            )
        ).scalar_one()
        or 0
    )

    recent = db.execute(
        select(ClickEvent.ts, ClickEvent.referrer, ClickEvent.ua)
        .where(ClickEvent.link_id == link.id)
        .order_by(ClickEvent.ts.desc())
        .limit(12)
    ).all()

    return {
        "code": link.code,
        "clicks": link.clicks or 0,
        "unique_visitors": int(unique or 0),
        "created_at": link.created_at,
        "last_click_at": last_click,
        "clicks_today": clicks_today,
        "daily": [{"date": k, "count": v} for k, v in buckets.items()],
        "top_referrers": top,
        "recent": [
            {
                "at": ts,
                "source": _host(ref) or "直接访问",
                "device": device_label(ua),
            }
            for ts, ref, ua in recent
        ],
    }


def device_label(ua: str | None) -> str:
    """把 UA 归成三类：桌面 / 移动 / 机器人。够用，不去猜操作系统。"""
    from app.core.guard import is_bot

    if is_bot(ua):
        return "机器人"
    low = (ua or "").lower()
    if not low:
        return "未知"
    mobile = ("mobile", "android", "iphone", "ipad", "ipod", "micromessenger", "mqqbrowser")
    return "移动端" if any(m in low for m in mobile) else "桌面端"


def bulk_action(db: Session, codes: list[str], action: str) -> dict:
    """批量启用 / 停用 / 删除。

    逐条处理而不是一条 SQL 全扫：短码可能写错、可能已被别人删掉，让好的一条条
    成功、坏的一条条报出来，比整批失败更有用。200 条上限在 schema 里挡着。
    """
    if action not in ("enable", "disable", "delete"):
        raise ServiceError("不支持的批量动作", 422)

    done = 0
    missing: list[str] = []
    for code in codes[:200]:
        row = db.execute(select(Link).where(func.lower(Link.code) == code.lower())).scalar_one_or_none()
        if row is None:
            missing.append(code)
            continue
        if action == "delete":
            db.delete(row)
        else:
            row.enabled = action == "enable"
        done += 1
    db.flush()
    logger.info("批量%s：成功 %d，缺失 %d", action, done, len(missing))
    return {"ok": True, "action": action, "done": done, "missing": missing[:20]}


def public_board(db: Session, limit: int | None = None) -> list[dict]:
    """公开热门榜：只吐已经在用的短码，不给后台开任何口子。

    列表里刻意不带目标地址与标签——访客本来就不会拥有「全部链接」的视野，
    这里只多给一个「最近被点得最多」的视角。
    """
    limit = max(1, min(20, int(limit if limit is not None else runtime.get_int("board_size"))))
    rows = (
        db.execute(
            select(Link)
            .where(Link.enabled.is_(True), Link.clicks > 0)
            .order_by(Link.clicks.desc())
            .limit(limit * 3)
        )
        .scalars()
        .all()
    )
    items = []
    for link in rows:
        # 过期 / 点满 / 目标被拉黑的条目对访客没有意义，别拿它占榜位
        if link.is_expired or link.exhausted or link.daily_exhausted:
            continue
        try:
            check_target_policy(link.target)
        except ServiceError:
            continue
        items.append(
            {
                "code": link.code,
                "title": link.title or link.target,
                "clicks": int(link.clicks or 0),
            }
        )
        if len(items) >= limit:
            break
    return items


def _host(referrer: str) -> str | None:
    if not referrer:
        return None
    from urllib.parse import urlparse

    try:
        h = urlparse(referrer).hostname
    except ValueError:
        return None
    return h or None
