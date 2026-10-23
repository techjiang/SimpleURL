"""YOURLS 数据迁移：解析 YOURLS 的 SQL 转储 / JSON 导出。"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("simpleurl")


def _normalize_ts(value) -> str | None:
    """YOURLS 的 timestamp 是 epoch 秒，转成 ISO 串。"""
    import datetime as dt

    try:
        ts = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _clean_url(url: str) -> str:
    return (url or "").strip()


def parse_json(text: str) -> list[dict]:
    """解析 YOURLS 的 JSON 导出（{keyword: url} 或 [{keyword,url,...}]）。"""
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValueError("不是合法的 JSON") from exc

    out: list[dict] = []
    if isinstance(data, dict):
        for keyword, url in data.items():
            if isinstance(url, str):
                out.append({"code": keyword, "target": _clean_url(url)})
            elif isinstance(url, dict):
                out.append({"code": keyword, "target": _clean_url(url.get("url", ""))})
    elif isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                continue
            code = row.get("keyword") or row.get("shorturl") or row.get("code")
            target = row.get("url") or row.get("longurl") or row.get("target")
            if code and target:
                out.append(
                    {
                        "code": str(code).strip(),
                        "target": _clean_url(str(target)),
                        "title": (row.get("title") or "").strip() or None,
                        "clicks": int(row.get("clicks") or 0),
                    }
                )
    return [r for r in out if r.get("code") and r.get("target")]


_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+`?yourls_url`?\s*\(([^)]*)\)\s*VALUES\s*(.+?);\s*$",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def _split_values(blob: str) -> list[str | None]:
    """按逗号切 VALUES 行，正确处理引号转义与 NULL。"""
    out: list[str | None] = []
    i, n = 0, len(blob)
    while i < n:
        while i < n and blob[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        if blob[i] == "'":
            i += 1
            buf: list[str] = []
            while i < n:
                ch = blob[i]
                if ch == "\\" and i + 1 < n:
                    buf.append(blob[i + 1])
                    i += 2
                    continue
                if ch == "'":
                    if i + 1 < n and blob[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(ch)
                i += 1
            out.append("".join(buf))
        else:
            j = i
            while j < n and blob[j] != ",":
                j += 1
            token = blob[i:j].strip()
            out.append(None if token.upper() == "NULL" else token)
            i = j
    return out


def parse_sql(text: str) -> list[dict]:
    """解析 YOURLS 的 SQL 转储（phpMyAdmin 风格）。"""
    out: list[dict] = []
    for match in _INSERT_RE.finditer(text or ""):
        columns = [c.strip().strip("`") for c in match.group(1).split(",")]
        for row_blob in re.findall(r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)", match.group(2)):
            values = _split_values(row_blob)
            if len(values) != len(columns):
                continue
            row = dict(zip(columns, values))
            code = row.get("keyword")
            target = row.get("url")
            if not code or not target:
                continue
            out.append(
                {
                    "code": str(code).strip(),
                    "target": _clean_url(str(target)),
                    "title": (row.get("title") or "").strip() or None,
                    "clicks": int(row.get("clicks") or 0),
                    "created_at": _normalize_ts(row.get("timestamp")),
                }
            )
    return out


def parse(text: str) -> list[dict]:
    """自动识别格式并解析。"""
    stripped = (text or "").strip()
    if not stripped:
        return []
    if stripped[:1] in "{[":
        return parse_json(stripped)
    if re.search(r"INSERT\s+INTO", stripped, re.IGNORECASE):
        return parse_sql(stripped)
    raise ValueError("无法识别的格式：请粘贴 YOURLS 的 SQL 转储或 JSON 导出")
