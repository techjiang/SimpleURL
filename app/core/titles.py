"""自动获取目标页标题。

创建链接时同步取一次 `<title>`，取不到就用 URL 兜底，绝不让标题拖垮创建。

三件事必须做对，否则这个功能只是「看起来有」：

- **真的会去取**。早期版本只有 `guess_title`，从没发过请求，页面上那个
  「自动获取标题」开关拨来拨去都没有区别。
- **在请求线程外跑**。创建接口是同步函数，直接 `asyncio.run()` 会撞上
  「事件循环已在运行」，被 `except` 一口吞掉，永远拿不到真标题。这里改成
  在独立线程里自建事件循环。
- **SSRF 与重定向两侧都看一眼**。域名解析出内网就拒收；跟随重定向时
  手动一跳一跳地过同一道闸——公网地址 302 到 169.254.169.254 是经典绕过。
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
import threading
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("simpleurl")

MAX_TITLE = 255
MAX_BYTES = 512 * 1024
MAX_REDIRECTS = 3

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")

# 一次创建最多等它这么久，超时当成「拿不到」
_CONNECT_TIMEOUT = 3.0


def _clean(raw: str) -> str:
    text = _TAG_RE.sub("", raw)
    # 实体必须解开：`<title>A &amp; B &#8212; C</title>` 抓回来会是
    # `A &amp; B &#8212; C`，Jinja 转义一交就原样显示成 `&amp;`，
    # 列表里一屏「Python 3.15.2 documentation」中间挂着 `&#8212;`。
    # 解两次：网页里 `&amp;#8212;` 这种双重转义也见过。
    text = html_lib.unescape(html_lib.unescape(text))
    text = _WS_RE.sub(" ", text).strip()
    return text[:MAX_TITLE]


def _is_safe_host(url: str) -> bool:
    """防 SSRF：只允许公网 http/https，挡掉内网地址。

    复用 security.is_public_url，和创建链接时走同一道判定——
    两套规则各写一遍，早晚会有一边漏。
    """
    from app.core.security import is_public_url

    return is_public_url(url)


def _decode(body: bytes, charset: str | None) -> str:
    for enc in (charset, "utf-8", "gb18030"):
        if not enc:
            continue
        try:
            return body.decode(enc, errors="replace")
        except (LookupError, TypeError):
            continue
    return body.decode("utf-8", errors="replace")


async def _fetch_once(client: httpx.AsyncClient, url: str) -> tuple[str | None, str | None]:
    """请求一次，返回 (标题, 重定向目标)。状态码 >=400 视为失败。"""
    resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; SimpleURL/1.0)"})
    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("location")
        return None, location
    if resp.status_code >= 400:
        return None, None
    html = _decode(resp.content[:MAX_BYTES], resp.charset_encoding)
    match = _TITLE_RE.search(html)
    if not match:
        return None, None
    return (_clean(match.group(1)) or None), None


async def fetch_title(url: str, timeout: int = 5) -> str | None:
    """拉取页面 `<title>`，失败一律返回 None（拿不到不算错）。"""
    from urllib.parse import urljoin

    current = url
    if not current or not _is_safe_host(current):
        return None
    limit = max(1, min(30, timeout))
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(limit, connect=min(_CONNECT_TIMEOUT, limit)),
            follow_redirects=False,  # 自己跟，每一跳都要过 SSRF 闸
            max_redirects=MAX_REDIRECTS,
            verify=False,
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                title, location = await _fetch_once(client, current)
                if title:
                    return title
                if not location:
                    return None
                current = urljoin(current, location)
                # 重定向目标是可被目标站控制的输入：跳到内网就立刻收手
                if not _is_safe_host(current):
                    logger.info("标题获取放弃：重定向到不安全地址 %s", current[:120])
                    return None
            return None
    except Exception as exc:  # 网络问题一律不阻塞主流程
        logger.debug("获取标题失败 %s: %s", url, exc)
        return None


def fetch_title_sync(url: str, timeout: int = 5) -> str | None:
    """在独立线程里跑 `fetch_title`。

    创建链接的链路（包括 FastAPI 的同步路由、以及被 `create_link` 复用的
    YOURLS 兼容接口）本来就在事件循环所在线程里跑，直接 `asyncio.run()`
    会抛 `RuntimeError: asyncio.run() cannot be called from a running event loop`，
    且异常会被上层宽泛的 except 吃掉 —— 表现就是「标题永远是兜底值」。
    """
    box: dict[str, object] = {}

    def _run() -> None:
        try:
            box["title"] = asyncio.run(fetch_title(url, timeout=timeout))
        except Exception as exc:  # noqa: BLE001 - 标题拿不到不影响创建
            box["error"] = exc

    # 硬超时：出网慢的时候不能把创建请求一起拖住
    worker = threading.Thread(target=_run, name="title-fetch", daemon=True)
    worker.start()
    worker.join(timeout + 2)
    if worker.is_alive():
        logger.debug("获取标题超时 %s", url)
        return None
    return box.get("title") if isinstance(box.get("title"), str) else None


# 纯数字 / 纯编号的路径段当不了标题。
# `https://site/-/issues/1` 兜底出来是「1」，列表里每一条都顶着个「1」——
# 比留空更难认，也让人以为「自动取标题」坏了。
_MEANINGLESS = re.compile(r"^(\d{1,6}|index|home|default|page)$", re.IGNORECASE)


def guess_title(url: str) -> str:
    """网络拿不到时，用 URL 最后一段兜底当标题。

    拿不出有意义的一段时返回**带域名的主机名**，而不是空串或一个数字：
    列表里「cnb.cool」比「1」有用得多，也比空标题强。
    """
    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
    except ValueError:
        return ""
    host = parsed.hostname or ""
    slug = path.rsplit("/", 1)[-1] if path else ""
    slug = slug.split("?", 1)[0].split("#", 1)[0]
    slug = re.sub(r"\.(html?|php|aspx?|jsp)$", "", slug, flags=re.IGNORECASE)
    slug = slug.replace("-", " ").replace("_", " ").strip()
    if not slug or _MEANINGLESS.match(slug):
        return host[:MAX_TITLE]
    return slug[:MAX_TITLE] or host[:MAX_TITLE]


def resolve_title(url: str, enabled: bool, timeout: int = 5) -> tuple[str | None, str]:
    """按开关取标题，返回 (标题, 来源)。

    来源是 `fetched` / `fallback` / `off`，后台「标题来源」列靠它说清
    这一条到底是抓来的还是兜底拼的。
    """
    if not enabled:
        return None, "off"
    fetched = fetch_title_sync(url, timeout=timeout)
    if fetched:
        return fetched, "fetched"
    return (guess_title(url) or None), "fallback"
