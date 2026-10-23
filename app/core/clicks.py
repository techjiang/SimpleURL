"""延迟计数：点击事件先进内存缓冲，再由后台线程批量落库。

直接写库的话，一次热门跳转就是一次 insert + 一次 update，高并发下会把
SQLite/Postgres 打满。这里改成「缓冲 + 批量 flush」，跳转接口只做入队，
写库压力摊到每个 flush 周期的一次事务里。
"""

from __future__ import annotations

import datetime as dt
import logging
import threading

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_engine, get_sessionmaker
from app.core.security import ip_hash
from app.core.timeutil import utcnow
from app.models import ClickEvent, Link

logger = logging.getLogger("simpleurl")


class _Pending:
    """一条待写入的点击。"""

    __slots__ = ("link_id", "referrer", "ua", "device", "ip", "blocked", "ts")

    def __init__(self, link_id: int, referrer, ua, device, ip, blocked, ts) -> None:
        self.link_id = link_id
        self.referrer = referrer
        self.ua = ua
        self.device = device
        self.ip = ip
        self.blocked = blocked
        self.ts = ts


class ClickBuffer:
    """内存缓冲 + 定时批量落库。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: list[_Pending] = []
        self._counts: dict[int, int] = {}
        self._dropped = 0
        self._timer: threading.Timer | None = None
        self._running = False

    # ---------- 生产 ----------
    def add(
        self,
        db: Session,
        link_id: int,
        referrer: str | None,
        ua: str | None,
        ip: str | None,
        blocked: bool = False,
    ) -> None:
        from app.core.security import parse_user_agent

        _, device = parse_user_agent(ua)
        item = _Pending(
            link_id=link_id,
            referrer=(referrer or "")[:512] or None,
            ua=(ua or "")[:512] or None,
            device=device,
            ip=ip,
            blocked=blocked,
            ts=utcnow(),
        )
        with self._lock:
            self._queue.append(item)
            # 被 UA 屏蔽的事件只留痕、不算点击
            if not blocked:
                self._counts[link_id] = self._counts.get(link_id, 0) + 1

    # ---------- 观测 ----------
    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def pending_for(self, link_id: int) -> int:
        """某个链接还没落库的点击数。"""
        with self._lock:
            return self._counts.get(link_id, 0)

    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    # ---------- 落库 ----------
    def flush(self) -> int:
        """把缓冲一次性写进数据库，返回写入条数。"""
        with self._lock:
            batch = self._queue
            self._queue = []
            counts = self._counts
            self._counts = {}
        if not batch:
            return 0

        session = None
        try:
            session = get_sessionmaker()()
            session.add_all(
                [
                    ClickEvent(
                        link_id=item.link_id,
                        ts=item.ts,
                        referrer=item.referrer,
                        ua=item.ua,
                        device=item.device,
                        ip_hash=ip_hash(item.ip) or None,
                        blocked=item.blocked,
                    )
                    for item in batch
                ]
            )
            for link_id, delta in counts.items():
                session.execute(
                    Link.__table__.update()
                    .where(Link.id == link_id)
                    .values(clicks=func.coalesce(Link.clicks, 0) + delta)
                )
            session.commit()
            logger.info("延迟计数 flush：写入 %d 条点击", len(batch))
            return len(batch)
        except Exception:
            if session is not None:
                session.rollback()
            # 写库失败就退回队列，别把数据丢了
            with self._lock:
                self._queue = batch + self._queue
                for link_id, delta in counts.items():
                    self._counts[link_id] = self._counts.get(link_id, 0) + delta
            logger.exception("延迟计数 flush 失败，已回退队列")
            raise
        finally:
            if session is not None:
                session.close()

    def flush_all(self) -> int:
        """进程退出前调用：反复 flush 直到队列清空。"""
        total = 0
        for _ in range(10):
            if self.pending() == 0:
                break
            total += self.flush()
        return total

    # ---------- 定时任务 ----------
    def start(self, interval: int) -> None:
        if self._running:
            return
        self._running = True

        def _tick() -> None:
            try:
                self.flush()
            except Exception:
                pass
            self._timer = threading.Timer(interval, _tick)
            self._timer.daemon = True
            self._timer.start()

        self._timer = threading.Timer(interval, _tick)
        self._timer.daemon = True
        self._timer.start()
        logger.info("延迟计数已启动，flush 周期 %ds", interval)

    def stop(self) -> None:
        self._running = False
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def reset(self) -> None:
        with self._lock:
            self._queue = []
            self._counts = {}
            self._dropped = 0


click_buffer = ClickBuffer()


def pending_clicks(db: Session, code: str) -> int:
    """还没落库的点击数（管理后台展示「延迟计数」状态用）。"""
    from app.services import get_link_by_code

    link = get_link_by_code(db, code)
    return click_buffer.pending_for(link.id)


def flush_now() -> int:
    """手动触发一次落库（后台「立即结算」按钮用）。"""
    return click_buffer.flush()
