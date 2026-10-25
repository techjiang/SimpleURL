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


def local_naive(value: dt.datetime | None) -> dt.datetime | None:
    """转成浏览器 datetime-local 输入框能吃的 naive 本地时间（YYYY-MM-DDTHH:mm）。"""
    aware = ensure_aware(value)
    if aware is None:
        return None
    return aware.astimezone().replace(tzinfo=None)


def fmt_local(value: dt.datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    aware = ensure_aware(value)
    return aware.astimezone().strftime(fmt) if aware else ""


def local_date(value: dt.datetime | None) -> dt.date | None:
    """转成本地日期。

    统计按天分桶时不能用 UTC 日期：容器默认跑 UTC，国内用户看到的
    「今天」会比服务器早 8 小时——早上 7 点前的点击全部落到昨天那张柱子上。
    入库统一 UTC，出库统一本地，中间别再各写一遍。
    """
    aware = ensure_aware(value)
    return aware.astimezone().date() if aware else None
