"""安全相关：URL 校验、IP 取址、限速。"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import ipaddress
import logging
import re
import socket
import threading
import time
from urllib.parse import urlparse

from app.core.config import settings

logger = logging.getLogger("simpleurl")

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
    """校验 URL：协议、长度、主机、是否指向内网。

    重点挡两类坑：
    - `https://user@host` 形式的 userinfo：钓鱼页最爱，域名显示和实际访问不是一回事
    - 内网 / 回环 / 链路本地地址：短链被当成 SSRF 跳板去摸内网服务
    """
    if not url or len(url) > MAX_URL_LENGTH:
        return False
    if url != url.strip():
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return False
    # 带上账号信息的一律不收，防止 https://trusted.com@evil.com 这种伪装
    if parsed.username or parsed.password or "@" in url.split("//", 1)[-1].split("/", 1)[0]:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or "." not in host:
        return False
    if host in BLOCKED_HOSTS or host.endswith(".local") or host.endswith(".internal") or host.endswith(".localhost"):
        return False
    if any(ord(ch) < 0x20 or ch in "<>\"{}|\\^`" for ch in url):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return is_public_ip(ip)
    # 域名也得看一眼解析结果：只校验字面 IP 的话，
    # `http://10.0.0.1.nip.io` 这种自带解析的域名能直接绕过整道 SSRF 防线
    if not settings.ssrf_resolve_check:
        return True
    return not resolves_to_internal(host)


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """公网地址才算安全。内网 / 回环 / 链路本地 / 保留段一律不收。"""
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        return False
    if ip.is_unspecified:
        return False
    # IPv4 映射的 IPv6 地址（::ffff:127.0.0.1）等畸形写法
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and not is_public_ip(mapped):
        return False
    return True


_dns_cache: dict[str, tuple[float, bool]] = {}
_dns_lock = threading.Lock()


def resolves_to_internal(host: str) -> bool:
    """域名解析出的任一地址落在内网就判不安全（失败时按不安全处理）。

    结果缓存几十秒：同一域名被反复贴进来时不用每次都问 DNS，
    但也不能长效缓存——DNS 重绑定攻击要的正是「第一次解析是公网」。
    """
    from app.core.config import settings

    ttl = getattr(settings, "dns_cache_ttl", 60)
    now = time.monotonic()
    if ttl > 0:
        with _dns_lock:
            hit = _dns_cache.get(host)
            if hit and now - hit[0] < ttl:
                return hit[1]
    result = _resolve_internal_uncached(host)
    if ttl > 0:
        with _dns_lock:
            if len(_dns_cache) > 4096:
                _dns_cache.clear()
            _dns_cache[host] = (now, result)
    return result


_DNS_WORKERS = 4
_dns_pool: concurrent.futures.ThreadPoolExecutor | None = None
_dns_pool_lock = threading.Lock()


def _dns_executor() -> concurrent.futures.ThreadPoolExecutor:
    """专用小线程池：解析慢的时候不能连累别的请求。

    不用默认执行器——那是全站共用的，被 DNS 卡住的线程会把
    模板渲染、日志之类的东西一起排到后面去。
    """
    global _dns_pool
    if _dns_pool is not None:
        return _dns_pool
    with _dns_pool_lock:
        if _dns_pool is None:
            _dns_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=_DNS_WORKERS, thread_name_prefix="dns"
            )
        return _dns_pool


def _resolve_addrs(host: str) -> list[str]:
    """域名 -> 地址列表。在专用线程里跑，调用方负责掐超时。"""
    return [info[4][0] for info in socket.getaddrinfo(host, None) if len(info) > 4 and info[4][0]]


def _getaddrinfo_bounded(host: str) -> list[str] | None:
    """带超时的 getaddrinfo。返回 None 表示解析失败或超时。"""
    from app.core.config import settings

    timeout = max(0.1, min(10.0, float(getattr(settings, "dns_timeout", 2.0) or 2.0)))
    try:
        future = _dns_executor().submit(_resolve_addrs, host)
    except RuntimeError:  # 线程池已关闭（进程退出中）
        return None
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        # 线程还在跑，结果直接丢弃：这个域名这一轮按不安全处理
        future.cancel()
        logger.info("域名解析超时（>%.1fs）：%s", timeout, host[:120])
        return None
    except (socket.gaierror, UnicodeError, OSError):
        return None
    except Exception:  # noqa: BLE001 - 解析异常一律按不安全处理
        return None


def _resolve_internal_uncached(host: str) -> bool:
    """解析域名，任一条结果落在内网就判不安全。

    必须有硬超时：`getaddrinfo` 是同步阻塞调用，域名服务器不回话时
    要等系统解析器的 timeout（默认 5 秒 × 重试次数）。它跑在创建链接的
    请求线程里，于是「DNS 半死不活」的时候，每一次创建都要先卡几秒，
    并发一上来就把同步接口的线程池占满——整个站点跟着变慢，
    而页面上只看到「创建有点慢」，没人会想到是解析超时。
    """
    addrs = _getaddrinfo_bounded(host)
    if addrs is None:
        # 解析失败 / 超时：按不安全处理，别把「解析不出来」当「公网」放过去
        return True
    seen = False
    for addr in addrs:
        if not addr:
            continue
        try:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError:
            continue
        seen = True
        if not is_public_ip(ip):
            return True
    # 一条地址都没解析出来：按不安全处理，别把「解析失败」当「公网」放过去
    return not seen


def ip_hash(ip: str | None) -> str:
    if not ip:
        return ""
    return hashlib.sha256(f"simpleurl::{ip}".encode()).hexdigest()[:32]


# 常见 UA 片段 -> 可读名称（按序匹配，先长后短）
_UA_TOKENS: list[tuple[str, str]] = [
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
    ("MicroMessenger/", "微信"),
    ("DingTalk/", "钉钉"),
    ("QQ/", "QQ 浏览器"),
    ("curl/", "curl"),
    ("Wget/", "wget"),
    ("python-requests", "requests"),
    ("python-httpx", "httpx"),
]

_MOBILE_TOKENS = ("Android", "iPhone", "iPad", "iPod", "Mobile", "HarmonyOS", "Windows Phone")
_BOT_TOKENS = ("bot", "spider", "crawl", "scrapy", "HeadlessChrome", "python-requests", "curl/")


def parse_user_agent(ua: str | None) -> tuple[str, str]:
    """把 User-Agent 拆成 (浏览器/客户端名, 设备类型)。"""
    raw = (ua or "").strip()
    if not raw:
        return "未知", "unknown"
    lowered = raw.lower()
    if any(t.lower() in lowered for t in _BOT_TOKENS):
        return "机器人 / 脚本", "bot"
    name = "其他"
    for token, label in _UA_TOKENS:
        if token.lower() in lowered:
            name = label
            break
    if any(t.lower() in lowered for t in _MOBILE_TOKENS):
        device = "mobile"
    elif "Windows" in raw or "Macintosh" in raw or "X11" in raw or "CrOS" in raw:
        device = "desktop"
    else:
        device = "other"
    return name, device


def _trusted_proxies() -> set[str]:
    raw = settings.trusted_proxies
    if raw == "*":
        return {"*"}
    return {p.strip() for p in raw.split(",") if p.strip()}


def _is_trusted_proxy(ip: str | None) -> bool:
    """转发头只有来自受信代理时才认，否则一律按直连地址算。

    不设这道关的话，任何人都能伪造 X-Forwarded-For 把限速和每日配额绕过去。
    """
    trusted = _trusted_proxies()
    if not trusted:
        return False
    if "*" in trusted:
        return True
    if not ip:
        return False
    if ip in trusted:
        return True
    # 兼容写成网段的形式，如 10.0.0.0/8
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in trusted:
        if "/" not in entry:
            continue
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request) -> str | None:
    """取客户端 IP：默认只认直连地址，转发头需代理在信任列表里。

    X-Forwarded-For 从**右往左**找第一个不在信任列表里的地址。
    取最左边那个（`split(",")[0]`）看着更简单，但那个位置是客户端自己填的：
    请求一路穿过反代时，Nginx 只会在末尾追加，不会洗掉前面 —— 于是
    `X-Forwarded-For: 1.2.3.4` 后面跟几千个不同 IP，限速和每日配额就形同虚设。
    右边才是最近的、由我们自己这层代理写进去的地址。
    """
    peer = request.client.host if request.client else None
    if not _is_trusted_proxy(peer):
        return peer
    fwd = request.headers.get("x-forwarded-for") or ""
    hops = [part.strip() for part in fwd.split(",") if part.strip()]
    for hop in reversed(hops):
        if not _is_trusted_proxy(hop):
            return hop
    if hops:
        # 整条链都是自己的代理：取最左，也就是最初的发起方
        return hops[0]
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip() or peer
    return peer


class RateLimiter:
    """极简内存滑动窗口限速器（按 key 计数）。

    两件事必须做对，否则限速只是看起来在工作：
    - **原子性**：先读后写在并发下会把上限放大好几倍，热链接上尤其明显
    - **自然日配额**：`window` 给 86400 不代表「今日」，滚动 24 小时
      会让昨晚刷满的人在今天早上还继续被拦。`daily=True` 按自然日重置。
    """

    def __init__(self, clock=None) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._clock = clock or time.time
        self._last_sweep = 0.0

    def allow(self, key: str, limit: int, window_seconds: int = 60, daily: bool = False) -> bool:
        # limit <= 0 一律视为不限制：0 是「关闭限速」的约定值，
        # 不能理解成「一次都不许过」
        if limit <= 0:
            return True
        now = self._clock()
        with self._lock:
            existing = self._hits.get(key)
            if daily and existing and self._day_of(existing[-1]) != self._day_of(now):
                # 跨自然日：昨天的配额一笔勾销（空 bucket 也一起清掉，别留垃圾）
                self._hits.pop(key, None)
            since = self._day_start(now) if daily else now - window_seconds
            bucket = [t for t in self._hits.get(key, []) if t >= since]
            if len(bucket) >= limit:
                self._hits[key] = bucket
                return False
            bucket.append(now)
            self._hits[key] = bucket
            self._sweep(now)
            return True

    @staticmethod
    def _day_start(ts: float) -> float:
        moment = dt.datetime.fromtimestamp(ts)
        return moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    @staticmethod
    def _day_of(ts: float) -> dt.date:
        return dt.datetime.fromtimestamp(ts).date()

    def _sweep(self, now: float) -> None:
        """每 10 分钟清一次过期 key，长跑进程的内存不能只涨不落。"""
        if now - self._last_sweep < 600:
            return
        self._last_sweep = now
        horizon = now - 86400
        for key in [k for k, v in self._hits.items() if not v or v[-1] < horizon]:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


rate_limiter = RateLimiter()
