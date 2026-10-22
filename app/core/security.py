"""安全相关：URL 校验、IP 哈希、限速。"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import time
from urllib.parse import urlparse

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
    if not ip:
        return ""
    return hashlib.sha256(f"simpleurl::{ip}".encode()).hexdigest()[:32]


def client_ip(request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else None


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
