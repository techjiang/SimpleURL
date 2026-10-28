"""延迟计数：点击事件先进内存缓冲，再由后台线程批量落库。

直接写库的话，一次热门跳转就是一次 insert + 一次 update，高并发下会把
SQLite/Postgres 打满。这里改成「缓冲 + 批量 flush」，跳转接口只做入队，
写库压力摊到每个 flush 周期的一次事务里。
"""

from __future__ import annotations

import datetime as dt
import logging
import threading

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_engine, get_sessionmaker
from app.core.security import ip_hash
from app.core.timeutil import today_local, utcnow
from app.models import ClickEvent, Link, LinkVisitor

logger = logging.getLogger("simpleurl")

# 队列里出现第一条点击后，最多再等这么久就把这一批写掉。
# 定时器是「多久检查一次」，不等于「多久落一次库」：只靠周期的话，
# 一条冷门链接的点击要等到下一个整点周期才可见，中间刷新页面看到的是 0。
DELAYED_FLUSH_SECONDS = 0.5


class _Pending:
    """一条待写入的点击。"""

    __slots__ = ("link_id", "referrer", "ua", "device", "platform", "ip", "blocked", "reason", "ts", "ip_hash")

    def __init__(self, link_id: int, referrer, ua, device, platform, ip, blocked, reason, ts, ip_digest="") -> None:
        self.link_id = link_id
        self.referrer = referrer
        self.ua = ua
        self.device = device
        self.platform = platform
        self.ip = ip
        self.blocked = blocked
        self.reason = reason
        self.ts = ts
        # 摘要算一次就够：下面既要用它判重，又要写进 click_events.ip_hash。
        # 未加盐的 sha256 截断对 IPv4 等于没脱敏，所以统一走 ip_hash()。
        self.ip_hash = ip_digest


class ClickBuffer:
    """内存缓冲 + 定时批量落库。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: list[_Pending] = []
        self._counts: dict[int, int] = {}
        # 还没落库的「今日额度占用」，与 _counts 同生共死
        self._daily: dict[int, int] = {}
        # 还没落库的「按天的访问者集合」：(link_id, 本地日期) -> {ip_hash}
        self._visitors: dict[tuple[int, object], set[str]] = {}
        self._dropped = 0
        self._timer: threading.Timer | None = None
        self._delay_timer: threading.Timer | None = None
        self._flush_lock = threading.Lock()
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
        platform: str | None = None,
        reason: str | None = None,
        schedule: bool = True,
        daily_limit: int = 0,
    ) -> bool:
        """入队一条点击；队列满时丢弃并返回 False。

        `schedule=False` 不排「稍后落库」：只有真正走完跳转的请求才需要
        立刻让数字可见。被拦下的访问本来就不进点击数，为它们多排一次
        落库，等于给刷量的人开了一条「每来一次就唤醒一次写库」的路。

        `daily_limit > 0` 时这一票同时占该链接今天的额度。额度必须和点击数
        在同一把锁里扣、并跟点击在同一批 flush 里落库：分成两处更新的话，
        同一秒进来的并发请求会集体看到「今天还剩 1 次」，集体放行，超额几乎必现。
        """
        from app.core.security import parse_user_agent

        _, device = parse_user_agent(ua)
        digest = ip_hash(ip) or ""
        item = _Pending(
            link_id=link_id,
            referrer=(referrer or "")[:512] or None,
            ua=(ua or "")[:512] or None,
            device=device,
            platform=(platform or None),
            ip=ip,
            blocked=blocked or bool(reason),
            reason=reason,
            ts=utcnow(),
            ip_digest=digest,
        )
        with self._lock:
            # 队列上限：数据库长时间写不进去时，内存不能跟着请求数一起涨。
            # 满了就丢最旧的（那些最可能已经过期了），丢多少记在 _dropped 上。
            if len(self._queue) >= settings.click_buffer_max:
                self._queue.pop(0)
                self._dropped += 1
            self._queue.append(item)
            # 被 UA 屏蔽 / 平台拦截 / 超出配额的事件只留痕、不算点击
            if not blocked and not reason:
                self._counts[link_id] = self._counts.get(link_id, 0) + 1
                if digest:
                    key = (link_id, today_local())
                    bucket = self._visitors.get(key)
                    if bucket is None:
                        bucket = set()
                        self._visitors[key] = bucket
                    bucket.add(digest)
                if daily_limit > 0:
                    self._daily[link_id] = self._daily.get(link_id, 0) + 1
        if schedule:
            self._schedule_delayed()
        return True

    def _add_daily_usage(self, session: Session, daily: dict[int, int]) -> None:
        """把这一批占用的今日额度累加进 links.daily_used。

        跨日了就直接从 0 起算：`daily_date` 不是今天就等于昨天那本账已经翻篇，
        没必要再开个定时任务去清零——那种任务在多实例下会重复跑，漏一次就永久卡住。
        """
        if not daily:
            return
        # 日期必须是**本地**自然日：判定侧（Link.daily_used_today）比的是
        # utcnow().date()，而页面文案写的是「明天零点恢复」。两边都按 UTC 切，
        # 东八区的用户看到的截止点是本地早上 8 点——和文案差一整个上午。
        today = today_local()
        table = Link.__table__
        for link_id, delta in daily.items():
            session.execute(
                table.update()
                .where(table.c.id == link_id)
                .values(
                    daily_used=case(
                        (table.c.daily_date == today, func.coalesce(table.c.daily_used, 0) + delta),
                        else_=delta,
                    ),
                    daily_date=today,
                )
            )

    def _add_visitors(self, session: Session, visitors: dict) -> None:
        """把这一批的「谁来过」写进 link_visitors，已存在的组合直接跳过。

        唯一访客以前是查点击明细算 `count(distinct ip_hash)`，可明细会被
        `click_events` 的保留期清掉（默认 180 天）——清完之后「独立访客」
        一格一格往下掉，而「总点击」不变。数字自己会缩水，比报错更难受。
        所以单独一张表按 (链接, 本地日期) 记住谁来过，明细清了也不影响。
        """
        if not visitors:
            return
        table = LinkVisitor.__table__
        for (link_id, day), digests in visitors.items():
            if not digests:
                continue
            existing = set(
                session.execute(select(table.c.ip_hash).where(table.c.link_id == link_id, table.c.day == day)).scalars()
            )
            fresh = [
                {"link_id": link_id, "day": day, "ip_hash": digest}
                for digest in digests
                if digest and digest not in existing
            ]
            if not fresh:
                continue
            if session.bind is not None and session.bind.dialect.name == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as _insert

                session.execute(_insert(table).values(fresh).on_conflict_do_nothing())
            else:
                session.execute(table.insert().values(fresh))

    # ---------- 观测 ----------
    def pending_daily(self, link_id: int) -> int:
        """某条链接还没落库的今日额度占用。"""
        with self._lock:
            return self._daily.get(link_id, 0)

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def pending_for(self, link_id: int) -> int:
        """某个链接还没落库的点击数。"""
        with self._lock:
            return self._counts.get(link_id, 0)

    def pending_visitors(self, link_id: int) -> int:
        """某个链接今天还没落库的独立访客数（按 IP 摘要去重）。"""
        with self._lock:
            return len(self._visitors.get((link_id, today_local()), ()))

    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    # ---------- 清理 ----------
    def discard(self, link_id: int) -> int:
        """丢掉某条链接还没落库的点击，返回丢掉几条。

        用在彻底删除之后：缓冲里那些事件指向的 `link_id` 已经不存在了，
        而 SQLite 会把让出来的号发给下一条新链接——不清的话，新建的短链
        一出来就带着几次别人的点击，统计页上的来源也对不上。
        """
        with self._lock:
            before = len(self._queue)
            self._queue = [item for item in self._queue if item.link_id != link_id]
            dropped = before - len(self._queue)
            self._counts.pop(link_id, None)
            self._daily.pop(link_id, None)
            for key in [k for k in self._visitors if k[0] == link_id]:
                self._visitors.pop(key, None)
        return dropped

    # ---------- 落库 ----------
    def flush(self) -> int:
        """把缓冲一次性写进数据库，返回写入条数。

        全程持 `_flush_lock`：定时器、延迟合并、后台「立即结算」按钮、
        退出结算可能同时触发。两批并发写库不会丢数据（失败那批会退回队列），
        但会把同一个链接的两笔 `clicks = clicks + n` 拆成两个事务——
        SQLite 上就是多一次写锁争抢，而写锁正是这套设计要避开的东西。
        """
        with self._flush_lock:
            return self._flush_locked()

    def _flush_locked(self) -> int:
        with self._lock:
            batch = self._queue
            self._queue = []
            counts = self._counts
            self._counts = {}
            daily = self._daily
            self._daily = {}
            visitors = self._visitors
            self._visitors = {}
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
                        platform=item.platform,
                        ip_hash=item.ip_hash or None,
                        blocked=item.blocked,
                        block_reason=item.reason,
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
            self._add_daily_usage(session, daily)
            self._add_visitors(session, visitors)
            session.commit()
            if self._dropped:
                logger.warning("延迟计数期间因队列超限丢弃 %d 条点击", self._dropped)
                self._dropped = 0
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
                for link_id, delta in daily.items():
                    self._daily[link_id] = self._daily.get(link_id, 0) + delta
                for key, digests in visitors.items():
                    self._visitors.setdefault(key, set()).update(digests)
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
    def _schedule_delayed(self) -> None:
        """第一条点击入队时排一次「稍后落库」。

        同一个窗口里再来多少条都不会重复排——只留一个定时器。
        不排的话，落库节奏完全由 `CLICK_FLUSH_INTERVAL` 决定：
        默认 5 秒的周期里，前 4.9 秒的点击都是「看不见」的，
        用户点完立刻刷新列表，看到的是 0，还得靠「待结算」标记自己解释。
        """
        if not self._running:
            return
        if self._delay_timer is not None:
            return
        timer = threading.Timer(DELAYED_FLUSH_SECONDS, self._delayed_flush)
        timer.daemon = True
        self._delay_timer = timer
        timer.start()

    def _delayed_flush(self) -> None:
        self._delay_timer = None
        if not self._running and self.pending() == 0:
            return
        try:
            self.flush()
        except Exception:  # noqa: BLE001 - 数据已退回队列，下一轮定时器会再试
            pass

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
        for attr in ("_timer", "_delay_timer"):
            timer = getattr(self, attr, None)
            if timer is not None:
                timer.cancel()
                setattr(self, attr, None)

    def reset(self) -> None:
        if self._delay_timer is not None:
            self._delay_timer.cancel()
            self._delay_timer = None
        with self._lock:
            self._queue = []
            self._counts = {}
            self._daily = {}
            self._visitors = {}
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
