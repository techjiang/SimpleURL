"""安全相关：URL 校验、IP 哈希、限速。"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import time
from urllib.parse import urlparse

from app.core.config import settings

ALLOWED_SCHEMES = {"http", "https"}
BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}
MAX_URL_LENGTH = 2048

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def normalize_url(raw: str) -> str:
    """补全协议、去首尾空白，不做强校验。"""
    if raw is None:
        return ""
    url = str(raw).strip()
    if url and not _URL_RE.match(url):
        url = "https://" + url.lstrip("/")
    return url


def is_public_url(url: str) -> bool:
    """校验 URL：协议、长度、域名格式。"""
    if not url or len(url) > MAX_URL_LENGTH:
        return False
    if url != url.strip():
        return False
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return False
    host = (parsed.hostname or "").lower()
    if not host or "." not in host:
        return False
    if host in BLOCKED_HOSTS or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved):
        return False
    if any(ord(ch) < 0x20 or ch in "<>\"{}|\\^`" for ch in url):
        return False
    return True


def ip_hash(ip: str | None) -> str:
    """访客 IP 的单向摘要。

    加了 SECRET_KEY 当盐：没有盐的 SHA-256 前缀对 IP 来说等于没脱敏——
    IPv4 空间只有 2^32，离线枚举一张表就能反查。改了 SECRET_KEY 等于历史
    访客标识整体换一遍，独立访客会一次性重算，代价可接受。
    """
    if not ip:
        return ""
    return hashlib.sha256(f"simpleurl::{settings.secret_key}::{ip}".encode()).hexdigest()[:32]


def client_ip(request) -> str | None:
    """取客户端 IP。

    反代头（X-Forwarded-For / X-Real-IP）默认**不采信**：任何人都能伪造它，
    采信了就等于给限速和 IP 配额开了个后门。只有把反代地址写进
    TRUSTED_PROXIES 后，才从这些头里取真实客户端地址。
    """
    direct = request.client.host if request.client else None
    if not settings.trusted_proxies or direct not in settings.trusted_proxies:
        return direct

    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        # 取最右侧一个非信任地址，那是反代真实看到的客户端
        for candidate in reversed([h.strip() for h in fwd.split(",") if h.strip()]):
            if candidate not in settings.trusted_proxies:
                return candidate
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return direct


class RateLimiter:
    """极简内存滑动窗口限速器（按 key 计数）。"""

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        now = time.monotonic()
        bucket = [t for t in self._hits.get(key, []) if now - t < window_seconds]
        if len(bucket) >= limit:
            self._hits[key] = bucket
            return False
        bucket.append(now)
        self._hits[key] = bucket
        return True

    def reset(self) -> None:
        self._hits.clear()


rate_limiter = RateLimiter()


# ---------------- 域名过滤（域名级防护）----------------
_WILDCARD_RE = re.compile(r"^\*\.(.+)$")


def _norm_host(host: str) -> str:
    return (host or "").strip().lower().rstrip(".")


def parse_rule_list(raw) -> list[str]:
    """把多行 / 逗号分隔的文本拆成规则数组，顺手清掉重复。"""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = str(raw).replace(",", "\n").splitlines()
    out: list[str] = []
    for item in items:
        rule = item.strip().lower()
        if rule and rule not in out:
            out.append(rule)
    return out


def host_match(host: str, rule: str) -> bool:
    """域名匹配。`*.a.com` 只匹配子域，不匹配 a.com 本身——这点与 UA 通配不同。"""
    host = _norm_host(host)
    rule = _norm_host(rule)
    if not host or not rule:
        return False
    wildcard = _WILDCARD_RE.match(rule)
    if wildcard:
        suffix = wildcard.group(1)
        return host != suffix and host.endswith("." + suffix)
    # 裸域名覆盖它自己和所有子域：屏蔽 evil.com 却放过 a.evil.com 是自欺欺人
    return host == rule or host.endswith("." + rule)


def host_in(url: str, rules: list[str]) -> str | None:
    """URL 的主机命中任一规则就返回命中的那条规则。"""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return None
    for rule in rules:
        if host_match(host, rule):
            return rule
    return None


# ---------------- 短链目标的黑白名单 ----------------
LIST_MODE_OFF = "off"
LIST_MODE_BLOCK = "block"
LIST_MODE_ALLOW = "allow"


def target_policy(url: str, block: list[str], allow: list[str], mode: str) -> str | None:
    """判断目标域名是否符合站点名单。命中限制则返回人话原因。"""
    try:
        host = _norm_host(urlparse(url).hostname or "")
    except ValueError:
        return "地址不合法"
    if not host:
        return "地址缺少域名"
    if block and host_in(url, block):
        return f"{host} 已列入屏蔽名单"
    if mode == LIST_MODE_ALLOW and allow and not host_in(url, allow):
        return f"{host} 不在允许名单内"
    return None
