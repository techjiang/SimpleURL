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
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    # 浏览器内置 XSS 过滤器反而可能被利用，直接关掉
    "X-XSS-Protection": "0",
}


def security_headers(csp: str | None = None) -> dict[str, str]:
    headers = dict(_SECURITY_HEADERS)
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
        for key, value in security_headers(csp).items():
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
