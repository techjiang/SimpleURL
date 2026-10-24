"""防洪：限速、UA 屏蔽、微信/QQ 内打开处理。

短链服务最怕两件事：有人拿它当跳板刷量，以及一条链接在微信群里被引爆。
这里把两层分开处理——

- **限速**：滑动窗口计数，超限不是慢一点，而是直接挡在门外（429 页面）。
- **UA / 微信 / QQ**：判断的是「这次访问可信吗、能正常打开吗」，
  命中 UA 屏蔽仍然放行跳转，只是不计入统计，并在后台留痕；微信/QQ 内打开则
  进入兼容模式——这两个 App 的内置浏览器会拦截不少落地页，与其让用户看到
  白屏，不如给一张带「在浏览器中打开」提示的中转页。
"""

from __future__ import annotations

import fnmatch
import logging
import threading
import time

from app.core import runtime
from app.core.config import settings
from app.core.security import ip_hash

logger = logging.getLogger("simpleurl")

# ---- UA 判定 ----
WECHAT_MARKERS = ("micromessenger", "wechat")
QQ_MARKERS = ("qq/", "mqqbrowser", "qqbrowser")

# 这些 UA 一看就是脚本/爬虫在刷，默认不参与计数
_BOT_MARKERS = (
    "bot", "spider", "crawler", "curl/", "wget/", "python-requests",
    "go-http-client", "headlesschrome", "phantomjs", "scrapy",
)


def ua_lower(ua: str | None) -> str:
    return (ua or "").lower()


def is_wechat(ua: str | None) -> bool:
    low = ua_lower(ua)
    return any(marker in low for marker in WECHAT_MARKERS)


def is_qq(ua: str | None) -> bool:
    low = ua_lower(ua)
    if is_wechat(ua):
        # 微信里也可能带 QQ 浏览器内核标记，微信优先
        return False
    return any(marker in low for marker in QQ_MARKERS)


def is_bot(ua: str | None) -> bool:
    low = ua_lower(ua)
    return any(marker in low for marker in _BOT_MARKERS)


def match_ua(ua: str | None, patterns: list[str]) -> str | None:
    """UA 命中黑名单则返回命中的规则，否则 None。支持 * 通配与子串。"""
    if not ua:
        return None
    low = ua_lower(ua)
    for pattern in patterns:
        rule = pattern.strip()
        if not rule:
            continue
        rule_low = rule.lower()
        if "*" in rule_low or "?" in rule_low:
            if fnmatch.fnmatchcase(low, rule_low):
                return rule
        elif rule_low in low:
            return rule
    return None


# ---- 滑动窗口限速 ----
class SlidingWindow:
    """按 key 计数的滑动窗口。带封禁期，超限后一段时间直接拒绝。"""

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def allow(self, key: str, limit: int, window: int, block: int = 0) -> bool:
        """limit<=0 表示不限制。"""
        if limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            until = self._blocked_until.get(key)
            if until and until > now:
                return False
            if until:
                self._blocked_until.pop(key, None)
            bucket = [t for t in self._hits.get(key, []) if now - t < window]
            if len(bucket) >= limit:
                self._hits[key] = bucket
                if block > 0:
                    self._blocked_until[key] = now + block
                return False
            bucket.append(now)
            self._hits[key] = bucket
            self._sweep(now)
            return True

    def _sweep(self, now: float) -> None:
        """每 60 秒清一次陈旧的 key，避免长跑进程内存只涨不落。"""
        if now - self._last_sweep < 60:
            return
        self._last_sweep = now
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] > 3600]:
            self._hits.pop(key, None)
        for key in [k for k, until in self._blocked_until.items() if until <= now]:
            self._blocked_until.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._blocked_until.clear()


flood = SlidingWindow()


class GuardResult:
    """一次跳转请求的风控结论。"""

    __slots__ = ("allowed", "reason", "detail", "count_click", "interstitial")

    def __init__(
        self,
        allowed: bool = True,
        reason: str = "",
        detail: str = "",
        count_click: bool = True,
        interstitial: str = "",
    ):
        self.allowed = allowed
        self.reason = reason
        self.detail = detail
        self.count_click = count_click
        self.interstitial = interstitial  # wechat / qq / ua，用于决定渲染哪种中转页

    @property
    def blocked(self) -> bool:
        return not self.allowed


def check(code: str, ip: str | None, ua: str | None) -> GuardResult:
    """在跳转前调用，给出是否放行、是否计数、是否需要中转页。"""
    per_minute = runtime.get_int("guard_ip_per_minute")
    daily = runtime.get_int("guard_ip_daily")
    per_code = runtime.get_int("guard_code_per_minute")
    block = runtime.get_int("guard_block_seconds")

    client = ip or "unknown"
    # 单 IP 每分钟
    if not flood.allow(f"ipm:{client}", per_minute, 60, block):
        return GuardResult(False, "rate", f"单 IP 每分钟上限 {per_minute}")
    # 单 IP 每日
    if not flood.allow(f"ipd:{client}", daily, 86400, 0):
        return GuardResult(False, "rate", f"单 IP 每日配额 {daily}")
    # 单短码每分钟
    if not flood.allow(f"cpm:{code}", per_code, 60, 0):
        return GuardResult(False, "rate", f"单短码每分钟上限 {per_code}")

    if runtime.get_bool("blocked_ua_enabled"):
        hit = match_ua(ua, runtime.blocked_uas())
        if hit:
            return GuardResult(True, "ua", f"命中 UA 规则：{hit}", count_click=False, interstitial="ua")

    mode = runtime.get_str("wechat_mode")
    if mode != "off" and is_wechat(ua):
        if mode == "blocked":
            return GuardResult(False, "wechat", "微信内打开被拦截", count_click=False)
        return GuardResult(True, "wechat", "微信内打开", interstitial="wechat")

    mode = runtime.get_str("qq_mode")
    if mode != "off" and is_qq(ua):
        if mode == "blocked":
            return GuardResult(False, "qq", "QQ 内打开被拦截", count_click=False)
        return GuardResult(True, "qq", "QQ 内打开", interstitial="qq")

    return GuardResult()


def note_ip(ip: str | None) -> str | None:
    return ip_hash(ip) or None


def env_snapshot() -> dict:
    return {
        "per_minute": runtime.get_int("guard_ip_per_minute"),
        "daily": runtime.get_int("guard_ip_daily"),
        "per_code": runtime.get_int("guard_code_per_minute"),
        "block": runtime.get_int("guard_block_seconds"),
        "wechat_mode": runtime.get_str("wechat_mode"),
        "qq_mode": runtime.get_str("qq_mode"),
        "ua_rules": len(runtime.blocked_uas()),
        "env_ua_rules": len(settings.blocked_uas),
    }
