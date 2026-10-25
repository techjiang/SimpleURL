"""跳转链路上的运行策略快照。

一次跳转原本要读四五次 settings 表（频率上限、社交总开关、平台策略、
UA 规则、点击配额），热门短码上这些读全是重复劳动，还都要抢同一把数据库连接。

这里把「几乎不变、改了希望立刻生效」的那几项合成一份快照，缓存几秒。
后台一保存就调 `invalidate()` 立刻失效——性能与即时生效两头都要。
"""

from __future__ import annotations

import threading
import time

from sqlalchemy.orm import Session

SNAPSHOT_TTL = 3.0

_lock = threading.Lock()
_cache: dict[str, object] = {}
_cached_at = 0.0


def invalidate() -> None:
    """配置一变就把快照打掉，下次请求重建。"""
    global _cached_at
    with _lock:
        _cache.clear()
        _cached_at = 0.0


def snapshot(db: Session) -> dict:
    """当前生效的跳转策略快照（最多缓存 SNAPSHOT_TTL 秒）。"""
    global _cached_at
    now = time.monotonic()
    with _lock:
        if _cache and now - _cached_at < SNAPSHOT_TTL:
            return _cache

    from app.core import guard
    from app.core.settings_store import effective

    fresh = {
        "redirect_rate_limit": int(effective(db, "redirect_rate_limit") or 0),
        "click_daily_limit": int(effective(db, "click_daily_limit") or 0),
        "social_guard": bool(effective(db, "social_guard")),
        "platforms": guard.platform_rules(db),
        "ua_rules": guard.blocked_ua_rules(db),
    }
    with _lock:
        _cache.clear()
        _cache.update(fresh)
        _cached_at = now
    return fresh
