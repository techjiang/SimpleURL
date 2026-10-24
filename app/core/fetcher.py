"""抓取目标页标题：带 SSRF 防护与超时。

只允许 http/https、只允许公开地址，重定向每一跳都重新校验——否则一个
`302 -> http://127.0.0.1:6379` 就能把内网捅穿。抓不到就用 URL 兜底，绝不因此让创建失败。
"""

from __future__ import annotations

import html
import ipaddress
import logging
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx

from app.core.security import is_public_url

logger = logging.getLogger("simpleurl")

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_MAX_BYTES = 256 * 1024
_MAX_REDIRECTS = 3
_TIMEOUT = 5.0


def _host_is_public(url: str) -> bool:
    """DNS 解析后再校验一次 IP，防 DNS rebinding。"""
    host = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        # 解析不了就交给 is_public_url 的静态判断，抓不到标题也不是错
        return True
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def _clean(title: str) -> str:
    text = html.unescape(title)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:255]


def fetch_title(url: str) -> str | None:
    """取页面 <title>，失败返回 None。绝不抛异常给调用方。"""
    if not is_public_url(url):
        return None

    current = url
    try:
        with httpx.Client(
            timeout=_TIMEOUT,
            follow_redirects=False,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; SimpleURLBot/1.0; +title-fetch)",
                "Accept": "text/html,application/xhtml+xml",
            },
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                if not is_public_url(current) or not _host_is_public(current):
                    return None
                resp = client.get(current)
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        return None
                    current = urljoin(current, location)
                    continue
                if resp.status_code >= 400:
                    return None
                ctype = resp.headers.get("content-type", "")
                if "html" not in ctype.lower():
                    return None
                body = resp.text[:_MAX_BYTES]
                match = _TITLE_RE.search(body)
                if not match:
                    return None
                title = _clean(match.group(1))
                return title or None
    except Exception as exc:  # 网络问题一概不算错
        logger.debug("抓取标题失败 %s：%s", url, exc)
        return None
    return None
