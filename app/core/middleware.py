"""安全响应头与 CSRF 防护。

安全响应头走中间件，对所有响应统一生效，避免每个路由各写一遍。
CSRF 用双提交令牌：页面加载时服务端下发一个可读 cookie，前端所有写请求
（POST/PUT/PATCH/DELETE）都在 X-CSRF-Token 头里回带同一个值。攻击者能
诱导浏览器带上 cookie，但读不到 cookie 内容，所以伪造不出这个头。

静态资源与 /yourls-api 兼容接口不参与 CSRF 校验：前者没有副作用，
后者的调用方是第三方客户端，本来就不带我们的 cookie。
"""

from __future__ import annotations

import hmac
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.crypto import random_token

logger = logging.getLogger("simpleurl")

CSRF_COOKIE = "su_csrf"
CSRF_HEADER = "x-csrf-token"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
# 不参与 CSRF 校验的路径前缀
_CSRF_EXEMPT_PREFIXES = ("/static/", "/yourls-api")

_SECURITY_HEADERS = {
    # 禁 MIME 嗅探、点击劫持，referer 只回源站
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    # 资源侧单独判断，见 static_headers()
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    # 浏览器内置 XSS 过滤器反而可能被利用，直接关掉
    "X-XSS-Protection": "0",
}

# 允许被外站直接引用的站内资源：二维码、图标、样式表、脚本。
# 短链站的日常就是「把东西贴到别人的页面里」——微信 / QQ / Slack 抓预览
# 卡片、博客里嵌统计页的二维码，走的是 <img> 而不是导航请求。
# 统一按 same-origin 封掉的话，这些图全部变成裂图，而浏览器只在控制台
# 打一行 CORP 报错，从页面上看不出任何原因。
_ASSET_PREFIXES = ("/static/",)
_ASSET_EXACT = (
    "/favicon.ico",
    "/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png",
    "/api/qr/",
)


def is_embeddable_asset(path: str) -> bool:
    return path.startswith(_ASSET_PREFIXES) or path.startswith(_ASSET_EXACT)


def security_headers(csp: str | None = None, path: str = "") -> dict[str, str]:
    from app.core.config import settings

    headers = dict(_SECURITY_HEADERS)
    if settings.static_cross_origin and is_embeddable_asset(path):
        # 只放开「能不能被别站引用」，不放 frame-ancestors，也不碰页面本身
        headers["Cross-Origin-Resource-Policy"] = "cross-origin"
        # 顺带允许媒体/脚本跨域读取所需的 CORS 头，图标这种资源浏览器不校验，
        # 但 Canvas 里画二维码（导出分享图）需要。
        headers["Access-Control-Allow-Origin"] = "*"
    if csp:
        headers["Content-Security-Policy"] = csp
    return headers


def content_security_policy(nonce: str) -> str:
    """页面 CSP：脚本只放行带 nonce 的内联块与本站文件。

    站内没有第三方脚本、字体、图片，default-src 直接锁 'self'。
    style-src 保留 'unsafe-inline'：模板里有少量 style 属性，逐个改造收益不大。
    """
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "object-src 'none'",
            "upgrade-insecure-requests",
        ]
    )


def issue_token() -> str:
    """新的 CSRF 令牌。"""
    return random_token(16)


def _cookie_token(request: Request) -> str:
    return request.cookies.get(CSRF_COOKIE) or ""


def _is_csrf_exempt(path: str) -> bool:
    return path.startswith(_CSRF_EXEMPT_PREFIXES)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """统一补安全响应头，并给页面请求分配 CSP nonce 与 CSRF 令牌。"""

    async def dispatch(self, request: Request, call_next):
        token = _cookie_token(request) or issue_token()
        request.state.csrf_token = token
        request.state.csp_nonce = random_token(16)

        response = await call_next(request)

        # CSP 只挂在 HTML 上，API 的 JSON 响应不需要
        csp = None
        if response.headers.get("content-type", "").startswith("text/html"):
            csp = content_security_policy(request.state.csp_nonce)
        for key, value in security_headers(csp, request.url.path).items():
            response.headers[key] = value

        if _cookie_token(request) != token:
            response.set_cookie(
                CSRF_COOKIE,
                token,
                max_age=7 * 24 * 3600,
                httponly=False,  # 前端要读它并回带到请求头
                samesite="lax",
                path="/",
            )
        return response


ACCESS_COOKIE = "su_access"
# 领取码（回执）也落一份 cookie：
#   1. 统计页里的二维码是个 <img>，跨站上下文的图片请求带不了 `?t=`
#   2. 换一张回执之后浏览器地址要跳到新链接，这一跳同样会丢 `?t=`
# 只写自己那条回执的哈希对应关系由服务端校验，cookie 里放明文当「出示凭据」，
# 和 `?t=` 同级——它本来就是一张回执，不是密码。
CLAIM_COOKIE = "su_claim"


class AccessTokenCookieMiddleware(BaseHTTPMiddleware):
    """`?token=xxx` 打开过之后写进 cookie。

    前端门禁是拿着令牌拼 query 再跳转的，不落 cookie 的话每次刷新都得重输，
    二维码、统计页里那几张图也会各丢一次令牌。写进 HttpOnly cookie 之后，
    页面判定只看 cookie，但 ?token= 依然可用（脚本、书签、外部集成靠它）。
    """

    async def dispatch(self, request: Request, call_next):
        from app.core.config import settings

        expected = settings.access_token
        if expected:
            provided = request.query_params.get("token")
            if provided and hmac.compare_digest(provided, expected):
                request.cookies  # noqa: B018 - 确保 cookies 已解析
                # 把令牌挂到 request.state，后面的页面判定统一从这里取
                request.state.access_token_ok = True

        response = await call_next(request)

        # 回执：页面上带着 ?t= 访问过一次，就把它记进 cookie，
        # 这样同一条链接上的二维码、刷新、换新后的跳转都还认得出这位创建者
        # 两个来源都要记：页面带 `?t=` 打开一次，或者调用 `/receipt` 换到新的一张。
        # 只记前者的结果是「换新之后 cookie 里还是旧的」，刷新一次就 401。
        fresh_claim = getattr(request.state, "claim_token", None) or request.query_params.get("t")
        if fresh_claim:
            response.set_cookie(
                CLAIM_COOKIE,
                fresh_claim,
                max_age=30 * 24 * 3600,
                httponly=True,
                samesite="lax",
                path="/",
            )

        if expected:
            provided = request.query_params.get("token")
            if provided and hmac.compare_digest(provided, expected):
                response.set_cookie(
                    ACCESS_COOKIE,
                    expected,
                    max_age=30 * 24 * 3600,
                    httponly=True,
                    samesite="lax",
                    path="/",
                )
        return response


class CSRFProtectMiddleware(BaseHTTPMiddleware):
    """写请求必须回带 CSRF 令牌，且与 cookie 一致。"""

    async def dispatch(self, request: Request, call_next):
        if request.method not in _SAFE_METHODS and not _is_csrf_exempt(request.url.path):
            cookie = _cookie_token(request)
            header = request.headers.get(CSRF_HEADER, "")
            if not cookie or not header or not hmac.compare_digest(cookie, header):
                logger.warning("CSRF 校验失败：%s %s", request.method, request.url.path)
                return _reject(request)
        return await call_next(request)


def _reject(request: Request):
    from fastapi.responses import JSONResponse
    from starlette import status

    if request.url.path.startswith("/api/"):
        return JSONResponse({"ok": False, "message": "请求校验未通过，请刷新页面后重试"}, 403)
    return JSONResponse(
        {"ok": False, "message": "页面已过期，请刷新后重试"},
        status.HTTP_403_FORBIDDEN,
    )
