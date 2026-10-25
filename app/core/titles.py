"""自动获取目标页标题。

同步请求目标站会阻塞跳转链路，所以创建链接时只做「尽力而为」：
拿得到就用，拿不到就留空，绝不因为标题失败让创建失败。
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("simpleurl")

MAX_TITLE = 255
MAX_BYTES = 512 * 1024

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(raw: str) -> str:
    text = _TAG_RE.sub("", raw)
    text = _WS_RE.sub(" ", text).strip()
    return text[:MAX_TITLE]


def _is_safe_host(url: str) -> bool:
    """防 SSRF：只允许公网 http/https，挡掉内网地址。

    这里复用 security.is_public_url，和创建链接时走同一道判定——
    两套规则各写一遍，早晚会有一边漏。
    """
    from app.core.security import is_public_url

    return is_public_url(url)


async def fetch_title(url: str, timeout: int = 5) -> str | None:
    """拉取页面 <title>，失败一律返回 None。"""
    if not url or not _is_safe_host(url):
        return None
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, max_redirects=3, verify=False
        ) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; SimpleURL/1.0)"})
            if resp.status_code >= 400:
                return None
            body = resp.content[:MAX_BYTES]
            charset = resp.charset_encoding or "utf-8"
            try:
                html = body.decode(charset, errors="replace")
            except (LookupError, TypeError):
                html = body.decode("utf-8", errors="replace")
    except Exception as exc:  # 网络问题一律不阻塞主流程
        logger.debug("获取标题失败 %s: %s", url, exc)
        return None
    match = _TITLE_RE.search(html)
    if not match:
        return None
    title = _clean(match.group(1))
    return title or None


def guess_title(url: str) -> str:
    """网络拿不到时，用 URL 最后一段兜底当标题。"""
    try:
        path = urlparse(url).path.rstrip("/")
    except ValueError:
        return ""
    slug = path.rsplit("/", 1)[-1] if path else ""
    slug = slug.split("?", 1)[0].split("#", 1)[0]
    if not slug:
        host = urlparse(url).hostname or ""
        return host
    slug = re.sub(r"\.(html?|php|aspx?|jsp)$", "", slug, flags=re.IGNORECASE)
    return slug.replace("-", " ").replace("_", " ").strip()[:MAX_TITLE] or (urlparse(url).hostname or "")
