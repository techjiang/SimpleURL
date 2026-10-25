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


def local_day_start(day: dt.date) -> dt.datetime:
    """某个本地自然日的 00:00，换算成 UTC 时刻（库里一律存 UTC）。

    统计窗口、每日配额、仪表盘「今日」都要按本地自然日切，
    自己写 `combine(...,tzinfo=utc)` 会切在当地时间早上 8 点。
    """
    local = dt.datetime.combine(day, dt.time.min).astimezone()
    # 用真实时区库换算，别只靠 UTC 偏移：夏令时切换那几天偏移会变。
    # 容器里常见「UTC 偏移是 0，但 TZ=Asia/Shanghai」的组合，此时 astimezone()
    # 给的是 UTC 零点，和用户眼里的本地零点差 8 小时——所以 TZ 要单独认。
    zone = _process_zone()
    if zone is not None:
        try:
            return dt.datetime.combine(day, dt.time.min, tzinfo=zone).astimezone(dt.timezone.utc)
        except Exception:  # noqa: BLE001 - 时区库异常就退回按偏移算
            pass
    return local.astimezone(dt.timezone.utc)


def _process_zone():
    """进程实际使用的时区对象：优先认 TZ 环境变量，其次认系统本地时区。"""
    import os

    from zoneinfo import ZoneInfo

    key = (os.environ.get("TZ") or "").strip()
    if key and key not in {"UTC", "Etc/UTC"}:
        try:
            return ZoneInfo(key)
        except Exception:  # noqa: BLE001 - 写错的名字不该让页面挂掉
            return None
    name = getattr(dt.datetime.now().astimezone().tzinfo, "key", None)
    if name:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            return None
    return None


def today_local() -> dt.date:
    """今天的本地日期。统计窗口、每日配额都按它切。"""
    return dt.datetime.now(dt.timezone.utc).astimezone().date()
