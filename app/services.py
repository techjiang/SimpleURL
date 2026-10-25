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
from app.core.titles import resolve_title
from app.core.timeutil import ensure_aware, from_local_input, local_day_start, today_local, utcnow
from app.models import ClickEvent, Link
from app.schemas import LinkCreate, LinkUpdate

logger = logging.getLogger("simpleurl")

EXPIRING_DAYS = 7

# 一次点击没被计入的原因。库里存这几个短值，界面再翻译成人话。
NO_CLICK_UA = "ua"  # UA 黑名单命中
NO_CLICK_SOCIAL = "social"  # 社交平台策略拦下
NO_CLICK_SOCIAL_SILENT = "social_silent"  # 放行不计数
NO_CLICK_QUOTA = "quota"  # 单 IP 限速或每日点击配额用尽

NO_CLICK_LABELS = {
    NO_CLICK_UA: ("UA 屏蔽", "命中自定义 UA 黑名单，未计入点击数"),
    NO_CLICK_SOCIAL: ("平台拦截", "被社交平台防洪策略拦下，返回了提示页"),
    NO_CLICK_SOCIAL_SILENT: ("放行不计数", "页面正常打开了，但按策略不计入点击数"),
    NO_CLICK_QUOTA: ("超出配额", "单 IP 访问过于频繁或今日点击配额用完，未计入点击数"),
}

EXPORT_HEADER = ["短码", "短链接", "目标地址", "标题", "备注", "标签", "点击", "状态", "过期时间", "创建时间"]


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
    # 不带时区的输入是浏览器本地时间，不能当 UTC 收（会整体偏掉时区）
    value = from_local_input(value)
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
        # 库里已有同码时，大小写不同也要拦：域名不区分大小写，`Abc` 和 `abc`
        # 落到同一个地址上，谁后写谁赢，先建的人凭空被顶掉
        exists = db.execute(select(Link.code).where(func.lower(Link.code) == custom.lower())).first()
        if exists:
            raise ServiceError(f"短码 {custom} 已被占用（不区分大小写），换一个试试", 409)

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

    # 标题：用户填了就尊重用户的；没填才去抓，抓不到用 URL 兜底。
    # 开关既认后台配置，也认这次请求自带的 fetch_title（导入脚本常常不想等）。
    if not link.title:
        enabled = payload.fetch_title if payload.fetch_title is not None else bool(effective(db, "fetch_title"))
        title, source = resolve_title(link.target, enabled, timeout=settings.fetch_title_timeout)
        link.title = title
        link.title_source = source
    elif payload.title:
        link.title_source = "manual"
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


def _tag_filter(name: str):
    """按标签筛选，且认得「带引号的标签」。

    标签是 JSON 数组字符串，以前一律 `ilike '%"标签"%'`。可标签名里本来就
    允许出现 `"` 和 `\\`，存进库里是转义形式 `"a\\"b"`，直接拼原值永远匹配不上——
    界面上明明有这条标签，点进去却是空列表。所以两种写法都给：
    原样的（覆盖不需要转义的普通标签）+ JSON 转义后的（覆盖含引号 / 反斜杠的）。
    多这一个 or 只影响带特殊字符的标签，普通标签的命中结果不变。
    """
    from sqlalchemy import or_

    name = (name or "").strip()
    if not name:
        return Link.id.is_(None)
    # 原样的写法覆盖不需要转义的普通标签；JSON 转义（`json.dumps` 的输出去掉引号）
    # 覆盖含 `"` 与 `\` 的标签。两者都不加 escape 子句，跟 `_tag_filter` 出现之前一致。
    escaped = json.dumps(name, ensure_ascii=False)[1:-1]
    return or_(
        Link.tags.ilike(f'%"{name}"%'),
        Link.tags.ilike(f'%"{escaped}"%'),
    )


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
        stmt = stmt.where(_tag_filter(tag))
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


def list_tags(db: Session, limit: int = 60) -> list[dict]:
    """库里真实存在的标签，按出现次数排序。

    标签一直是「存得进去、筛不出来」：前台没有筛选入口，后台的下拉框写死了
    几个固定选项。要从 JSON 数组字符串里数，只能在应用层做一次聚合——
    链接量级是几千条，比给标签单开一张多对多表划算得多。
    """
    rows = db.execute(select(Link.tags).where(Link.tags.is_not(None), Link.tags != "")).scalars().all()
    counter: dict[str, int] = {}
    for raw in rows:
        try:
            items = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            name = str(item).strip()
            if name:
                counter[name] = counter.get(name, 0) + 1
    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    return [{"tag": name, "count": count} for name, count in ordered[:limit]]


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
    if "title" in data:
        link.title_source = "manual" if data["title"] else None
    db.flush()
    logger.info("更新短链 %s", link.code)
    return link


def renew_link(db: Session, link: Link, days: int = 30, enabled: bool = True) -> Link:
    """续期：从「现在」和「原到期时间」里取更晚的那个，再往后推 `days` 天。

    过期链接是最安静的失效方式——发出去半年后突然打不开，而人根本不知道。
    从原到期时间往后推，是为了让「还剩 3 天」的链接不会因为续一次 30 天
    反倒被提前扣掉这 3 天；已经过期很久的则一律从此刻算起。
    """
    days = max(1, min(3650, int(days or 30)))
    base = utcnow()
    if link.expires_at is not None:
        current = ensure_aware(link.expires_at)
        if current and current > base:
            base = current
    link.expires_at = base + dt.timedelta(days=days)
    if enabled:
        link.enabled = True
    db.flush()
    logger.info("续期短链 %s 至 %s", link.code, link.expires_at)
    return link


def delete_link(db: Session, code: str) -> None:
    link = get_link_by_code(db, code)
    db.delete(link)
    db.flush()
    logger.info("删除短链 %s", link.code)


BULK_ACTIONS = ("enable", "disable", "delete")


def _bulk_code(raw: object) -> str:
    """把界面上可能抄进来的各种写法收敛成纯短码。

    列表里显示的是 `/abc`，接口文档里的示例是完整的 `https://host/abc`，
    勾选后原样提交回来就成了「找不到这条链接」——批量操作静默失效，
    比报错更难受。这里统一去掉协议 + 主机 + 前导斜杠。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
        text = text.split("/", 1)[1] if "/" in text else ""
    return normalize_code(text.lstrip("/"))


def bulk_update(db: Session, codes: list[str], action: str) -> dict:
    """批量启用 / 停用 / 删除。不存在的短码跳过，不让一条脏码毁掉整批。"""
    if action not in BULK_ACTIONS:
        raise ServiceError("不支持的操作", 422)
    wanted = [_bulk_code(c) for c in codes]
    wanted = [c for c in wanted if c]
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
        click_buffer.add(db, link.id, referrer, ua, ip, platform=platform, reason=NO_CLICK_SOCIAL)
        return {"recorded": False, "blocked": True, "platform": platform, "reason": NO_CLICK_SOCIAL}

    blocked_rule = guard.is_ua_blocked(ua, snap.get("ua_rules") or [])
    if blocked_rule:
        click_buffer.add(db, link.id, referrer, ua, ip, platform=platform, reason=NO_CLICK_UA)
        logger.info("UA 屏蔽命中 %s -> %s", link.code, blocked_rule)
        return {"recorded": False, "blocked": True, "platform": platform, "reason": NO_CLICK_UA}

    allowed, reason = guard.check_click_quota(db, ip, snap)
    if not allowed:
        # 这一条原来连留痕都没有：跳转照常 307，库里一行记录都不落，
        # 于是「今天的点击数为什么比实际访问少了一大截」在界面上无从解释。
        # 现在同样入库（blocked + quota），只是不计入点击数。
        click_buffer.add(db, link.id, referrer, ua, ip, platform=platform, reason=NO_CLICK_QUOTA)
        logger.info("点击配额拦截 %s -> %s", link.code, reason)
        return {"recorded": False, "blocked": True, "reason": reason, "platform": platform, "block_reason": NO_CLICK_QUOTA}

    if platform and snap.get("social_guard") and (snap.get("platforms") or {}).get(platform) == "silent":
        # 放行不计数：怀疑被刷量的平台上，真实用户照样能打开，只是不进统计
        click_buffer.add(db, link.id, referrer, ua, ip, platform=platform, reason=NO_CLICK_SOCIAL_SILENT)
        return {"recorded": False, "blocked": True, "platform": platform, "reason": NO_CLICK_SOCIAL_SILENT}

    click_buffer.add(db, link.id, referrer, ua, ip, platform=platform)
    return {"recorded": True, "blocked": False, "platform": platform}


def get_stats(db: Session, code: str, days: int = 30) -> dict:
    link = get_link_by_code(db, code)
    days = max(1, min(365, days))
    # 分桶口径是本地日期，取窗口也得按本地自然日算。
    # 原来是「现在往前推 days-1 天」，本地 08:00 就是 UTC 00:00，
    # 于是「最近 30 天」实际是 29 天前到今天凌晨，今天的点击一根柱子都没有——
    # 图上一片空白，卡片上却明写着今天点了 23 次。
    today = today_local()
    since = local_day_start(today - dt.timedelta(days=days - 1))

    # 按本地日期分桶：UTC 分桶会让「今天」整体错位 8 小时（见 timeutil.local_date）。
    # 不用 SQL 的 "localtime" 修饰符：SQLite 里时间存的是不带时区的字符串，
    # `date(ts,'localtime')` 等于把这个 UTC 值当成 UTC 再转一次宿主机时区，
    # 而宿主机多半也是 UTC——分出来的日期跟「用户看到的今天」差一截。
    # 更隐蔽的是：容器时区设成东八区之后，同一个时刻会被分到前一天去。
    # 这里显式按 UTC 解释、再转进程本地时区，跟 Python 侧 today_local() 同一口径。
    local_day = func.date(func.datetime(ClickEvent.ts, "utc"), "localtime")
    daily_rows = db.execute(
        select(local_day, func.count(ClickEvent.id))
        .where(ClickEvent.link_id == link.id, ClickEvent.ts >= since)
        .group_by(local_day)
        .order_by(local_day)
    ).all()

    # 补全缺失日期
    buckets: dict[str, int] = {}
    for i in range(days):
        buckets[(today - dt.timedelta(days=days - 1 - i)).isoformat()] = 0
    for d, c in daily_rows:
        key = d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]
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
    # referrer 可能为空串以外的脏值（含端口的 origin、大写主机），统一收敛成主机名
    top_counts: dict[str, int] = {}
    for r, c in referrer_rows:
        host = _host(r)
        if host:
            top_counts[host] = top_counts.get(host, 0) + int(c)
    top = [{"source": k, "count": v} for k, v in sorted(top_counts.items(), key=lambda kv: -kv[1])]
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
        {
            "platform": social.label(key),
            "key": key or "other",
            "count": int(c),
            # 默认灰点毫无信息量，这里把墨迹色（社交平台官网色）一起给前端
            "color": social.color(key),
            "icon": social.icon(key),
        }
        for key, c in platform_rows
    ]
    blocked_count = db.execute(
        select(func.count(ClickEvent.id)).where(ClickEvent.link_id == link.id, ClickEvent.blocked.is_(True))
    ).scalar_one()
    # 「放行不计数」与「真被拦下」在库里都标 blocked，但对使用的人是两件事：
    # 前者用户其实打开了页面。拆开报，别让人以为微信那 27 次访问全被拒了。
    #
    # 口径改准：原来这里数的是「被拦且带平台」，凡是微信里被拦的都算成
    # 「放行不计数」，于是「平台拦截 10 次」里那 10 次也被重复报了一遍。
    # 现在只认 block_reason = social_silent，老数据没有这个字段才算 0。
    by_reason = {
        str(key or ""): int(count)
        for key, count in db.execute(
            select(ClickEvent.block_reason, func.count(ClickEvent.id))
            .where(ClickEvent.link_id == link.id, ClickEvent.blocked.is_(True))
            .group_by(ClickEvent.block_reason)
        ).all()
    }
    silent_count = by_reason.get(NO_CLICK_SOCIAL_SILENT, 0)
    no_click = [
        {
            "reason": key,
            "label": NO_CLICK_LABELS.get(key, ("其它", ""))[0],
            "hint": NO_CLICK_LABELS.get(key, ("其它", "没计入点击"))[1],
            "count": count,
        }
        # 老数据（这次升级前入库的）没有原因，归到「历史未分类」最后一栏，
        # 总不能因为升级把它们算丢
        for key, count in sorted(by_reason.items(), key=lambda kv: -kv[1])
        if key
    ]
    legacy = by_reason.get("", 0)
    if legacy:
        no_click.append({"reason": "legacy", "label": "历史未分类", "hint": "升级前入库的拦截记录", "count": legacy})

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
        "silent": int(silent_count or 0),
        # 没计入点击的访问按原因拆开：UA 屏蔽 / 平台拦截 / 放行不计数 / 超出配额
        "no_click": no_click,
    }


def links_to_csv(links: list[Link], base_url: str = "") -> str:
    """把链接列表导成 CSV 文本。

    `base_url` 给了就把「短链接」列写成完整地址（`https://s.example/abc1`），
    否则至少是个以 `/` 开头的路径。之前两列都是裸短码，导出后
    谁也看不出这条链接到底是什么。
    """

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
                _safe_cell(link.code),
                # 「短链接」这一列以前也填短码，导出来只看到 `abc1`，
                # 发给别人还得自己拼域名。这里补成完整地址
                _safe_cell(f"{base_url}/{link.code}" if base_url else f"/{link.code}"),
                _safe_cell(link.target),
                _safe_cell(link.title or ""),
                _safe_cell(link.note or ""),
                _safe_cell(_tags(link)),
                link.clicks or 0,
                _status(link),
                expires.strftime("%Y-%m-%d %H:%M") if expires else "",
                created.strftime("%Y-%m-%d %H:%M") if created else "",
            ]
        )
    return buf.getvalue()


# CSV 里以这几个字符开头的单元格会被 Excel / WPS / Numbers 当成公式执行。
# 危险之处在于：标题是任何人（开放模式下是访客）都能填的字段，管理员把导出
# 结果发给同事、对方双击打开，`=HYPERLINK(...)` 或 `=cmd|...` 就在本地跑起来了。
_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value: object) -> object:
    """中和 CSV 公式注入：前置一个单引号，Excel 会当成纯文本。"""
    if not isinstance(value, str) or not value:
        return value
    if value.startswith(_FORMULA_PREFIX):
        return "'" + value
    return value


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
    # 没计入点击的访问，按原因分账。「今天点击数怎么比实际访问少这么多」
    # 这个问题的答案全在这几个数字里：配额用尽、UA 命中、平台拦截，
    # 三者的处置方式完全不同，混成一个总数没法判断。
    no_click_rows = db.execute(
        select(ClickEvent.block_reason, func.count(ClickEvent.id))
        .where(ClickEvent.blocked.is_(True))
        .group_by(ClickEvent.block_reason)
    ).all()
    no_click = {key: 0 for key in NO_CLICK_LABELS}
    for key, count in no_click_rows:
        no_click[key if key in NO_CLICK_LABELS else "legacy"] = int(count)
    return {
        "links": link_count,
        "click_events": event_count,
        "db_size": db_size,
        "pending_clicks": click_buffer.pending(),
        "dropped_clicks": click_buffer.dropped(),
        "expiring_links": expiring,
        "retention_days": CLICK_EVENT_RETENTION_DAYS,
        "title_sources": title_source_stats(db),
        "no_click": no_click,
        "warnings": access_warnings() + title_warnings(db) + quota_warnings(no_click),
    }


def title_source_stats(db: Session) -> dict:
    """标题来源分布：抓取成功多少、兜底多少、手填多少。

    「自动取标题」这个开关以前从没真发过请求，界面上拨来拨去都没区别，
    谁都发现不了。有了这组数字，抓取占比长期是 0 就等于当场报警。
    """
    rows = db.execute(
        select(Link.title_source, func.count(Link.id)).group_by(Link.title_source)
    ).all()
    out = {"fetched": 0, "fallback": 0, "manual": 0, "off": 0, "unknown": 0}
    for key, count in rows:
        out[key if key in out else "unknown"] += int(count)
    return out


def title_warnings(db: Session) -> list[dict]:
    """自动取标题开着，却一条都没抓到时，直接说清楚。"""
    if not effective(db, "fetch_title"):
        return []
    stats = title_source_stats(db)
    if stats["fallback"] + stats["fetched"] < 3:
        return []
    if stats["fetched"] > 0:
        return []
    return [
        {
            "level": "warn",
            "text": "自动取标题已开启，但没有一条抓到真实标题（全部走了 URL 兜底）。"
            "多半是这台机器出不了网，或被 SSRF 解析检查挡住了。",
        }
    ]


def quota_warnings(no_click: dict) -> list[dict]:
    """配额用尽的占比高了就直说，别让人对着少掉的点击数瞎猜。

    单 IP 的每日点击配额是防刷的口子，配小了会把自己人也挡在外面。
    判断门槛取两条：绝对量超过 20 次，且在那条链接的访问里占了一成以上。
    """
    quota = int(no_click.get(NO_CLICK_QUOTA) or 0)
    total = sum(int(v or 0) for v in no_click.values())
    if quota < 20 or not total:
        return []
    if quota / total < 0.1:
        return []
    return [
        {
            "level": "warn",
            "text": f"有 {quota} 次访问因为单 IP 每日点击配额用尽而没计入点击。"
            "如果这是正常访客，去「防洪 / UA 屏蔽」把每日点击上限调大，或确认 TRUSTED_PROXIES 正确——"
            "反代没配好时，所有访客会共用同一个 IP 的配额。",
        }
    ]


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


def code_available(db: Session, raw: str) -> tuple[bool, str]:
    """自定义短码可用性预检：让前端边打边看，别等提交了才知道被占。"""
    code = normalize_code(raw)
    if not code:
        return False, "短码不能为空"
    ok, msg = validate_custom_code(code, settings.code_min_length, settings.code_max_length)
    if not ok:
        return False, msg
    # 与 create_link 同一套语义：大小写不敏感，`Abc` 和 `abc` 是同一个地址
    if _code_taken(db, code):
        return False, "该短码已被占用"
    return True, "可以用"


def _url_key(url: str) -> str:
    """归一化目标地址用于查重：忽略协议差异、末尾斜杠与 # 片段。"""
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return str(url).strip()
    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("", host, path, parts.query, ""))


def find_similar(db: Session, target: str, limit: int = 5) -> list[dict]:
    """找出指向同一地址的已有短码，按「能用且点击多」排序。"""
    from app.core.security import normalize_url

    normalized = normalize_url(target) or str(target).strip()
    key = _url_key(normalized)
    if not key:
        return []

    # 先用 host 粗筛一把，再在 Python 里精确比对，别把库整张拉进内存
    host = key.split("/", 1)[0]
    rows = db.execute(select(Link).where(func.lower(Link.target).contains(host)).limit(500)).scalars().all()
    hits = [row for row in rows if _url_key(row.target) == key]
    hits.sort(key=lambda r: (not r.enabled or r.is_expired, -(r.clicks or 0)))
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


def _code_taken(db: Session, code: str) -> bool:
    return db.execute(select(Link.id).where(func.lower(Link.code) == code.lower())).first() is not None


def _parse_iso(value: str):
    try:
        return ensure_aware(dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


# ---------- CSV 导入 ----------
#
# import_yourls 认的是 YOURLS 的 SQL 转储和 JSON 导出，可人手上真正流通的
# 是「我自己导出的那张表」——本系统导出的 CSV、Excel 里攒的一张清单。
# 这里复用同一套入库逻辑，只把解析这层换成表头模糊匹配：
# 中文表头（短码 / 目标地址）、英文表头（code / url / target）、
# 甚至只有两列没表头，都能猜出来。

CSV_CODE_ALIASES = ("短码", "短链接", "code", "keyword", "slug", "key")
CSV_TARGET_ALIASES = ("目标地址", "目标", "原链接", "长链接", "url", "target", "longurl", "long_url", "destination")
CSV_TITLE_ALIASES = ("标题", "title", "name")
CSV_TAG_ALIASES = ("标签", "tags", "tag")
CSV_CLICKS_ALIASES = ("点击", "clicks", "hits")
CSV_EXPIRE_ALIASES = ("过期时间", "expires_at", "expire", "expires")


def _match_header(name: str, aliases: tuple[str, ...]) -> bool:
    low = str(name or "").strip().lower()
    return any(low == a.lower() or (a.isascii() and a.lower() in low) for a in aliases)


def parse_links_csv(text: str) -> list[dict]:
    """把 CSV 文本解析成入库用的行。猜不出目标地址列就整体报错。"""
    raw = (text or "").lstrip("\ufeff").strip()
    if not raw:
        return []
    # 先按分隔符猜一次：逗号、制表符、分号都常见
    sample = raw.splitlines()[0]
    delimiter = ","
    for candidate in ("\t", ";", ","):
        if sample.count(candidate) > sample.count(delimiter):
            delimiter = candidate
    reader = csv.DictReader(io.StringIO(raw), delimiter=delimiter)
    if not reader.fieldnames:
        return []

    mapping: dict[str, str] = {}
    for field in reader.fieldnames:
        if not field:
            continue
        for key, aliases in (
            ("code", CSV_CODE_ALIASES),
            ("target", CSV_TARGET_ALIASES),
            ("title", CSV_TITLE_ALIASES),
            ("tags", CSV_TAG_ALIASES),
            ("clicks", CSV_CLICKS_ALIASES),
            ("expires_at", CSV_EXPIRE_ALIASES),
        ):
            if key in mapping:
                continue
            if _match_header(field, aliases):
                mapping[key] = field
                break

    rows: list[dict] = []
    if "target" not in mapping:
        # 没有表头（或者表头一个都不认识）：按位置猜。第二列像链接就取第二列，
        # 否则第一列。只有一列且像链接时，那一列就是目标地址。
        for line in raw.splitlines():
            cells = next(csv.reader([line], delimiter=delimiter), [])
            cells = [c.strip() for c in cells if c is not None]
            if not cells:
                continue
            guess = next((c for c in reversed(cells) if c.lower().startswith(("http://", "https://"))), None)
            if not guess:
                continue
            code_guess = cells[0] if cells[0] != guess else ""
            if code_guess and code_guess.lower().startswith(("http://", "https://")):
                code_guess = ""
            rows.append({"code": code_guess, "target": guess})
        return rows

    for item in reader:
        target = str(item.get(mapping["target"]) or "").strip()
        if not target:
            continue
        rows.append(
            {
                "code": str(item.get(mapping.get("code", ""), "") or "").strip(),
                "target": target,
                "title": str(item.get(mapping.get("title", ""), "") or "").strip(),
                "tags": str(item.get(mapping.get("tags", ""), "") or "").strip(),
                "clicks": str(item.get(mapping.get("clicks", ""), "") or "").strip(),
                "expires_at": str(item.get(mapping.get("expires_at", ""), "") or "").strip(),
            }
        )
    return rows


def import_links_csv(db: Session, text: str) -> dict:
    """导入 CSV 清单。

    和 YOURLS 迁移共用「冲突自动换码」的策略，但多做两件事：
    · 标题、标签、点击数、过期时间一起带进来（用户导出的表里本来就有）
    · 同一份文件里自己重复也拦，不然第二条会撞上刚建好的那条
    """
    rows = parse_links_csv(text)
    if not rows:
        raise ServiceError("没解析出任何记录：至少要有一列目标地址（表头写「目标地址」或 url）", 422)

    imported = renamed = skipped = failed = 0
    seen: set[str] = set()
    for row in rows:
        try:
            target = _ensure_target(row.get("target", ""))
        except ServiceError:
            failed += 1
            continue
        if target.lower() in seen:
            skipped += 1
            continue
        seen.add(target.lower())

        raw_code = normalize_code(row.get("code") or "")
        code = raw_code
        if code:
            ok, _msg = validate_custom_code(code, settings.code_min_length, settings.code_max_length)
            if not ok or _code_taken(db, code):
                code = ""
        if not code:
            code = generate_unique_code(code_length_value(db), settings.code_no_ambiguous)
            if raw_code:
                renamed += 1

        clicks_raw = str(row.get("clicks") or "").strip().replace(",", "")
        clicks = int(clicks_raw) if clicks_raw.isdigit() else 0
        tags = [t.strip() for t in str(row.get("tags") or "").replace("，", ",").split(",") if t.strip()]
        link = Link(
            code=code,
            target=target,
            title=str(row.get("title") or "").strip()[:255] or None,
            tags=json.dumps(tags[:10], ensure_ascii=False) if tags else None,
            clicks=clicks,
            expires_at=_parse_iso(row.get("expires_at") or ""),
            title_source="manual" if row.get("title") else None,
        )
        db.add(link)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            failed += 1
            continue
        imported += 1
    logger.info("CSV 导入：成功 %d，换码 %d，跳过 %d，失败 %d", imported, renamed, skipped, failed)
    return {"imported": imported, "renamed": renamed, "skipped": skipped, "failed": failed}
