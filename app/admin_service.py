"""后台聚合：仪表盘统计、API 密钥、YOURLS 迁移、防洪留痕查询。"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.timeutil import ensure_aware, iso, utcnow
from app.models import ApiKey, ClickEvent, GuardEvent, Link
from app.schemas import LinkCreate
from app.services import ServiceError, create_link

logger = logging.getLogger("simpleurl")


# ---------------- 仪表盘 ----------------
def dashboard(db: Session, days: int = 14) -> dict:
    days = max(7, min(90, days))
    now = utcnow()
    since = now - dt.timedelta(days=days - 1)

    total_links = int(db.execute(select(func.count()).select_from(Link)).scalar_one() or 0)
    enabled_links = int(
        db.execute(select(func.count()).select_from(Link).where(Link.enabled.is_(True))).scalar_one() or 0
    )
    total_clicks = int(db.execute(select(func.coalesce(func.sum(Link.clicks), 0))).scalar_one() or 0)

    clicks_today = int(
        db.execute(
            select(func.count()).select_from(ClickEvent).where(ClickEvent.ts >= now - dt.timedelta(days=1))
        ).scalar_one()
        or 0
    )
    clicks_since = int(
        db.execute(
            select(func.count()).select_from(ClickEvent).where(ClickEvent.ts >= since)
        ).scalar_one()
        or 0
    )
    visitors_since = int(
        db.execute(
            select(func.count(func.distinct(ClickEvent.ip_hash))).where(
                ClickEvent.ts >= since, ClickEvent.ip_hash.is_not(None)
            )
        ).scalar_one()
        or 0
    )

    # 趋势：按天补齐，缺的日子补 0，图表才不会断
    rows = db.execute(
        select(func.date(ClickEvent.ts), func.count(ClickEvent.id))
        .where(ClickEvent.ts >= since)
        .group_by(func.date(ClickEvent.ts))
    ).all()
    bucket = {str(d): int(c) for d, c in rows}
    daily = []
    for i in range(days):
        day = (now - dt.timedelta(days=days - 1 - i)).date().isoformat()
        daily.append({"date": day, "count": bucket.get(day, 0)})

    top_links = [
        {"code": link.code, "title": link.title, "clicks": int(link.clicks or 0), "target": link.target}
        for link in db.execute(select(Link).order_by(Link.clicks.desc()).limit(8)).scalars().all()
        if (link.clicks or 0) > 0
    ]

    ref_rows = db.execute(
        select(ClickEvent.referrer, func.count(ClickEvent.id))
        .where(ClickEvent.ts >= since, ClickEvent.referrer.is_not(None), ClickEvent.referrer != "")
        .group_by(ClickEvent.referrer)
        .order_by(func.count(ClickEvent.id).desc())
        .limit(8)
    ).all()
    sources = []
    for ref, count in ref_rows:
        host = urlparse(ref).hostname if ref else None
        if host:
            sources.append({"source": host, "count": int(count)})

    guard_rows = db.execute(
        select(GuardEvent.kind, func.count(GuardEvent.id))
        .where(GuardEvent.ts >= since)
        .group_by(GuardEvent.kind)
    ).all()
    guard_stats = {kind: int(count) for kind, count in guard_rows}

    return {
        "total_links": total_links,
        "enabled_links": enabled_links,
        "total_clicks": total_clicks,
        "clicks_today": clicks_today,
        "clicks_recent": clicks_since,
        "visitors_recent": visitors_since,
        "days": days,
        "daily": daily,
        "top_links": top_links,
        "sources": sources,
        "guard": guard_stats,
    }


def recent_guard_events(db: Session, limit: int = 20) -> list[dict]:
    rows = db.execute(select(GuardEvent).order_by(GuardEvent.ts.desc()).limit(limit)).scalars().all()
    return [
        {
            "ts": iso(row.ts),
            "code": row.code,
            "kind": row.kind,
            "detail": row.detail,
            "ua": (row.ua or "")[:80],
        }
        for row in rows
    ]


# ---------------- API 密钥 ----------------
def list_api_keys(db: Session) -> list[dict]:
    rows = db.execute(select(ApiKey).order_by(ApiKey.created_at.desc())).scalars().all()
    return [row.to_dict() for row in rows]


def revoke_api_key(db: Session, key_id: int) -> None:
    row = db.get(ApiKey, key_id)
    if row is None:
        raise ServiceError("密钥不存在", 404)
    row.revoked = True
    db.flush()


def delete_api_key(db: Session, key_id: int) -> None:
    row = db.get(ApiKey, key_id)
    if row is None:
        raise ServiceError("密钥不存在", 404)
    db.delete(row)
    db.flush()


# ---------------- YOURLS 迁移 ----------------
_YOURLS_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+`?yourls_url`?\s*\(([^)]*)\)\s*VALUES\s*(.+?);",
    re.IGNORECASE | re.DOTALL,
)
_YOURLS_COLS = ("keyword", "url", "title", "timestamp", "ip", "clicks")


def _split_sql_values(block: str) -> list[list[str]]:
    """把 `('a','b'),('c','d')` 切成二维列表。够用就行，不写完整 SQL 解析器。"""
    rows: list[list[str]] = []
    current: list[str] = []
    buf: list[str] = []
    in_str = False
    escape = False
    for ch in block:
        if in_str:
            if escape:
                buf.append(ch)
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "'":
                in_str = False
            else:
                buf.append(ch)
            continue
        if ch == "'":
            in_str = True
        elif ch == "(":
            current, buf = [], []
        elif ch == ",":
            if current is not None:
                current.append("".join(buf).strip())
                buf = []
        elif ch == ")":
            current.append("".join(buf).strip())
            rows.append(current)
            current, buf = [], []
        elif ch == ";" or ch == "\n":
            continue
        else:
            buf.append(ch)
    return [row for row in rows if row]


def parse_yourls(raw: str) -> list[dict]:
    """支持 YOURLS 的 SQL 转储与 JSON 导出两种格式。"""
    text = (raw or "").strip()
    if not text:
        raise ServiceError("内容为空，先粘贴 YOURLS 的 SQL 或 JSON 导出", 400)

    if text[0] in "[{":
        return _parse_json(text)
    return _parse_sql(text)


def _parse_json(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ServiceError(f"JSON 解析失败：{exc}", 400) from exc
    if isinstance(data, dict):
        data = data.get("links") or data.get("urls") or list(data.values())
    if not isinstance(data, list):
        raise ServiceError("JSON 结构不认识，需要链接数组", 400)
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "keyword": str(item.get("keyword") or item.get("code") or "").strip(),
                "url": str(item.get("url") or item.get("target") or "").strip(),
                "title": str(item.get("title") or "").strip(),
                "clicks": int(item.get("clicks") or 0),
            }
        )
    return out


def _parse_sql(text: str) -> list[dict]:
    out: list[dict] = []
    for match in _YOURLS_INSERT_RE.finditer(text):
        cols = [c.strip().strip("`") for c in match.group(1).split(",")]
        rows = _split_sql_values(match.group(2))
        for row in rows:
            if len(row) < 2:
                continue
            record = dict(zip(cols, row))
            out.append(
                {
                    "keyword": str(record.get("keyword", "")).strip(),
                    "url": str(record.get("url", "")).strip(),
                    "title": str(record.get("title", "")).strip(),
                    "clicks": int(record.get("clicks") or 0) if str(record.get("clicks", "")).isdigit() else 0,
                }
            )
    if not out:
        raise ServiceError("没解析到 yourls_url 的 INSERT 语句，确认导出的表名与格式", 400)
    return out


def import_yourls(db: Session, raw: str, conflict: str = "rename") -> dict:
    """导入 YOURLS 数据。

    `conflict=rename` 短码冲突就换一个（默认，绝不覆盖已有链接）；
    `skip` 直接跳过冲突项。
    """
    records = parse_yourls(raw)
    created = skipped = failed = 0
    samples: list[dict] = []

    for record in records[:2000]:
        target = record["url"]
        if not target:
            failed += 1
            continue
        try:
            payload = LinkCreate(
                target=target,
                code=record["keyword"] or None,
                title=record["title"] or None,
            )
            link = create_link(db, payload)
            if record["clicks"]:
                link.clicks = record["clicks"]
            created += 1
        except ServiceError as exc:
            if exc.status == 409 and conflict == "skip":
                skipped += 1
                continue
            if exc.status == 409:
                # 换码重试一次
                try:
                    link = create_link(db, LinkCreate(target=target, title=record["title"] or None))
                    if record["clicks"]:
                        link.clicks = record["clicks"]
                    created += 1
                    samples.append({"from": record["keyword"], "to": link.code})
                    continue
                except ServiceError:
                    failed += 1
                    continue
            failed += 1
            if len(samples) < 5:
                samples.append({"error": f"{record['keyword'] or record['url']}: {exc.message}"})
    db.commit()
    return {"parsed": len(records), "created": created, "skipped": skipped, "failed": failed, "samples": samples}


# ---------------- 数据维护 ----------------
def purge_clicks(db: Session, before_days: int = 180) -> int:
    """清理过期点击明细，只保留链接累计点击数。"""
    before_days = max(1, before_days)
    cutoff = utcnow() - dt.timedelta(days=before_days)
    result = db.execute(select(ClickEvent).where(ClickEvent.ts < cutoff)).scalars().all()
    for row in result:
        db.delete(row)
    db.flush()
    return len(result)


def export_links(db: Session) -> list[dict]:
    rows = db.execute(select(Link).order_by(Link.created_at.desc())).scalars().all()
    return [
        {
            "code": row.code,
            "target": row.target,
            "title": row.title,
            "tags": row.to_dict()["tags"],
            "clicks": row.clicks,
            "enabled": row.enabled,
            "expires_at": iso(ensure_aware(row.expires_at)),
            "created_at": iso(row.created_at),
        }
        for row in rows
    ]
