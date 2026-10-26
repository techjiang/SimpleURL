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


def from_local_input(value: dt.datetime | None) -> dt.datetime | None:
    """把浏览器 `datetime-local` 传来的本地时间换成 UTC。

    要命的地方：JSON 里 `2026-12-31T23:59` 会被 pydantic 解析成**不带时区**的
    datetime，以前直接 `replace(tzinfo=utc)` 当成 UTC 收了，于是国内用户填
    「12 月 31 日 23:59 过期」，实际却在北京时间 1 月 1 日 07:59 才失效，
    页面上也照样显示 07:59 —— 设过期的人和看结果的人都很笃定，只是差了 8 小时。

    带时区的输入（前端补了 +08:00 之后）按原样换算，不做二次猜测。
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(dt.timezone.utc)
    zone = _process_zone()
    if zone is not None:
        try:
            return value.replace(tzinfo=zone).astimezone(dt.timezone.utc)
        except Exception:  # noqa: BLE001 - 时区库异常就退回系统偏移
            pass
    return value.astimezone(dt.timezone.utc)


def _local_now() -> dt.datetime:
    """现在的本地时间（aware）。

    注意不能用 `utcnow().astimezone()`：那取的是**系统**时区，容器里通常是 UTC。
    `TZ=Asia/Shanghai` 而系统偏移是 0 是最常见的部署组合，只看偏移会差 8 小时。
    偏偏这种错只在跨零点的那几个小时里露出来——白天一切正常，凌晨的数据
    全部落到「昨天」，属于最难复现的那一类。
    """
    zone = _process_zone()
    if zone is not None:
        try:
            return dt.datetime.now(dt.timezone.utc).astimezone(zone)
        except Exception:  # noqa: BLE001 - 时区库异常就退回系统本地
            pass
    return dt.datetime.now(dt.timezone.utc).astimezone()


def local_naive(value: dt.datetime | None) -> dt.datetime | None:
    """转成浏览器 datetime-local 输入框能吃的 naive 本地时间（YYYY-MM-DDTHH:mm）。"""
    aware = ensure_aware(value)
    if aware is None:
        return None
    zone = _process_zone()
    if zone is not None:
        try:
            return aware.astimezone(zone).replace(tzinfo=None)
        except Exception:  # noqa: BLE001
            pass
    return aware.astimezone().replace(tzinfo=None)


def fmt_local(value: dt.datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    aware = ensure_aware(value)
    if aware is None:
        return ""
    zone = _process_zone()
    if zone is not None:
        try:
            return aware.astimezone(zone).strftime(fmt)
        except Exception:  # noqa: BLE001
            pass
    return aware.astimezone().strftime(fmt)


def local_date(value: dt.datetime | None) -> dt.date | None:
    """转成本地日期。

    统计按天分桶时不能用 UTC 日期：容器默认跑 UTC，国内用户看到的
    「今天」会比服务器早 8 小时——早上 7 点前的点击全部落到昨天那张柱子上。
    入库统一 UTC，出库统一本地，中间别再各写一遍。
    """
    aware = ensure_aware(value)
    if aware is None:
        return None
    zone = _process_zone()
    if zone is not None:
        try:
            return aware.astimezone(zone).date()
        except Exception:  # noqa: BLE001
            pass
    return aware.astimezone().date()


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


def utc_offset_sql() -> str:
    """当前本地时区相对 UTC 的偏移，SQLite 修饰符格式，如 `+8 hours` / `-5 hours`。

    为什么不直接用 SQLite 的 `'localtime'`：它把查找时区的工作交给 C 库，
    而 C 库那次初始化发生在**进程启动时**，之后 `TZ` 再怎么变它都不理会。
    容器里「系统跑 UTC、应用配 TZ=Asia/Shanghai」是最常见的部署方式，
    于是 `date(ts,'localtime')` 出来的还是 UTC 日期——统计图表上今天的柱子
    永远是 0，而且只在本地零点之后的那几个小时里看得出来。

    自己算偏移就没这问题：口径由 `_process_zone()` 决定，和 Python 侧的
    `today_local()` / `local_day_start()` 完全一致。
    """
    return _offset_sql_at(_local_now())


def _offset_sql_at(moment: dt.datetime) -> str:
    """某个时刻在当前本地时区下的偏移。

    先把时刻换算到本地时区再取 `utcoffset()`：对一个 `UTC` 时区对象来说，
    它永远是 0——直接调等于什么都没算，夏令时地区一年到头都拿不到正确的偏移。
    """
    zone = _process_zone()
    if zone is not None:
        try:
            moment = moment.astimezone(zone)
        except Exception:  # noqa: BLE001 - 时区库异常就退回系统本地
            moment = moment.astimezone()
    else:
        moment = moment.astimezone()
    offset = moment.utcoffset()
    if offset is None:
        return "+0 hours"
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 60} hours" if total % 60 == 0 else f"{sign}{total // 60} hours {sign}{total % 60} minutes"


def _day_sql(column, offset: str):
    """UTC 时间列按给定偏移切日期：先把 naive 字符串标成 UTC，再平移。"""
    from sqlalchemy import func

    return func.date(func.datetime(column, offset))


def local_day_sql(column):
    """把 UTC 时间列按当前本地时区切成日期。

    不能拿 `utc_offset_sql()` 一条偏移套一年：夏令时地区一年里换两次偏移，
    3 月到 11 月之间分组出来全是差一小时的错位——图表上看着正常，
    只是跨零点那部分点击被算进了前一天或后一天，谁也说不清少的那几笔去哪了。
    这里按日期用 `CASE WHEN` 逐日算偏移：三百多个分支看着多，都是常量比较，
    SQLite 跑起来和单个 CASE 差不多，而结果每天都是对的，包括换季那天。
    """
    from zoneinfo import ZoneInfoNotFoundError

    from sqlalchemy import case

    try:
        today = today_local()
        # 逐日算偏移的输入只有「今天」和时区，两样都正常才值得展开这三百个分支
        local_day_start(today)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # 镜像里连时区库都没有时退回单条偏移：每年差一小时也好过整页报错
        return _day_sql(column, utc_offset_sql())
    branches = [
        (
            (today + dt.timedelta(days=delta)).isoformat(),
            _offset_sql_at(local_day_start(today + dt.timedelta(days=delta))),
        )
        for delta in range(-2, 367)
    ]
    first_label, first_offset = branches[0]
    rest = branches[1:]
    return case(
        *[(column < first_label, _day_sql(column, first_offset))]
        + [(column < label, _day_sql(column, offset)) for label, offset in rest],
        else_=_day_sql(column, rest[-1][1]),
    )


def today_local() -> dt.date:
    """今天的本地日期。统计窗口、每日配额都按它切。"""
    return _local_now().date()
