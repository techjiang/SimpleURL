"""时间工具：统一转成带时区的 UTC 时间。

SQLite 的 DateTime(timezone=True) 读回来是 naive 的，直接和 aware 时间比较会抛
TypeError，所以所有时间字段入库/出库都走这里归一化。
"""

from __future__ import annotations

import datetime as dt


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def ensure_aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def iso(value: dt.datetime | None) -> str | None:
    aware = ensure_aware(value)
    return aware.isoformat() if aware else None
