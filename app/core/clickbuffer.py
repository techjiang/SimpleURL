"""延迟计数：点击先入内存队列，攒批落库。

热链接被打爆时，原来的「一次点击一次 insert + 一次 update」能把 SQLite 写满。
这里把写入折叠成事务：攒够 `CLICK_FLUSH_BATCH` 条或过了 `CLICK_FLUSH_INTERVAL` 秒
才统一落库，一批只走一次事务。

取舍写在明面上——进程被 kill -9 时，缓冲里还没落库的那几秒会丢。要绝对不丢
就把 `CLICK_FLUSH_INTERVAL` 调到 1 秒；正常退出（含 SIGTERM）会强制结算一次。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from sqlalchemy import select, update

from app.core import runtime
from app.models import ClickEvent, Link

logger = logging.getLogger("simpleurl")


@dataclass
class _Pending:
    link_id: int
    ts: float
    daily_limit: int | None = None
    referrer: str | None = None
    ua: str | None = None
    ip_hash: str | None = None
    country: str | None = None


@dataclass
class Stats:
    queued: int = 0
    flushed: int = 0
    batches: int = 0
    dropped: int = 0
    last_flush: float = field(default=0.0)


class ClickBuffer:
    def __init__(self) -> None:
        self._items: list[_Pending] = []
        self._lock = threading.Lock()
        self._stats = Stats()
        # 当天已占用额度（内存态），由 _write 与点击明细一起落库
        self._daily_used: dict[int, int] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 入队 ----
    def add(self, link_id: int, referrer: str | None = None, ua: str | None = None,
            ip_hash: str | None = None, daily_limit: int | None = None) -> None:
        """入队一次点击。

        `daily_limit` 不为空时同时占用当天的每日额度：额度按**内存里已经入队**的条数算，
        不查库。否则同一秒里的并发请求会集体看到「今天还剩 1 次」，把额度超卖一整天。
        """
        item = _Pending(
            link_id=link_id,
            daily_limit=daily_limit,
            ts=time.time(),
            referrer=(referrer or "")[:512] or None,
            ua=(ua or "")[:512] or None,
            ip_hash=ip_hash,
        )
        with self._lock:
            self._items.append(item)
            self._stats.queued += 1
            if item.daily_limit:
                self._daily_used[link_id] = self._daily_used.get(link_id, 0) + 1
            over = len(self._items) >= runtime.get_int("click_flush_batch")
        if over:
            self.flush()

    def daily_used(self, link_id: int, persisted: int = 0) -> int:
        """当天已用额度 = 已落库的 + 还在缓冲里没落的。"""
        with self._lock:
            return int(persisted) + self._daily_used.get(link_id, 0)

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._items)

    def stats(self) -> Stats:
        """queued 表示当前还在缓冲区、尚未落库的条数。"""
        with self._lock:
            return Stats(
                queued=len(self._items),
                flushed=self._stats.flushed,
                batches=self._stats.batches,
                dropped=self._stats.dropped,
                last_flush=self._stats.last_flush,
            )

    # ---- 落库 ----
    def flush(self) -> int:
        with self._lock:
            if not self._items:
                return 0
            batch, self._items = self._items, []
        try:
            written = _write(batch)
        except Exception as exc:  # 写失败整批退回，不丢数据
            logger.warning("点击批量落库失败，%d 条退回队列：%s", len(batch), exc)
            with self._lock:
                self._items[:0] = batch
                if len(self._items) > 50000:  # 兜底，防止库长时间不可用把内存吃光
                    dropped = len(self._items) - 50000
                    del self._items[:dropped]
                    self._stats.dropped += dropped
            return 0
        with self._lock:
            # 落库成功后把内存里的额度计数减掉，不然会一直叠加
            for item in batch:
                if not item.daily_limit:
                    continue
                left = self._daily_used.get(item.link_id, 0) - 1
                if left > 0:
                    self._daily_used[item.link_id] = left
                else:
                    self._daily_used.pop(item.link_id, None)
            self._stats.flushed += written
            self._stats.batches += 1
            self._stats.last_flush = time.time()
        return written

    # ---- 后台线程 ----
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="click-flush", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(max(1, int(runtime.get_int("click_flush_interval")))):
            self.flush()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        # 退出前强制结算，尽量不丢
        self.flush()

    def reset(self) -> None:
        with self._lock:
            self._items.clear()
            self._daily_used.clear()
            self._stats = Stats()


def _write(batch: list[_Pending]) -> int:
    from app.core.db import session_scope

    counts: dict[int, int] = {}
    daily: dict[int, int] = {}
    for item in batch:
        counts[item.link_id] = counts.get(item.link_id, 0) + 1
        if item.daily_limit:
            daily[item.link_id] = daily.get(item.link_id, 0) + 1
    with session_scope() as db:
        for item in batch:
            db.add(
                ClickEvent(
                    link_id=item.link_id,
                    ts=_to_dt(item.ts),
                    referrer=item.referrer,
                    ua=item.ua,
                    ip_hash=item.ip_hash,
                    country=item.country,
                )
            )
        # 一次事务里把点击数加上去，比逐条 update 省得多
        for link_id, delta in counts.items():
            db.execute(update(Link).where(Link.id == link_id).values(clicks=Link.clicks + delta))
        _add_daily_usage(db, daily)
        db.flush()
    return len(batch)


def _add_daily_usage(db, daily: dict[int, int]) -> None:
    """把每日额度一起记进库。跨天自动从 1 重新计，不需要定时任务清零。"""
    if not daily:
        return
    import datetime as dt

    from sqlalchemy import case

    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    for link_id, delta in daily.items():
        db.execute(
            update(Link)
            .where(Link.id == link_id)
            .values(
                daily_used=case((Link.daily_date == today, Link.daily_used + delta), else_=delta),
                daily_date=today,
            )
        )


def _to_dt(ts: float):
    import datetime as dt

    return dt.datetime.fromtimestamp(ts, dt.timezone.utc)


def flush() -> int:
    """立即结算缓冲（测试与 graceful shutdown 用）。"""
    return click_buffer.flush()


def link_clicks(link_id: int) -> int:
    """从库里读真实点击数（缓冲里未结算的不算，统计以落库为准）。"""
    from app.core.db import session_scope

    with session_scope() as db:
        return int(db.execute(select(Link.clicks).where(Link.id == link_id)).scalar_one_or_none() or 0)


click_buffer = ClickBuffer()
