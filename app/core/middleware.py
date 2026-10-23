"""安全中间件：安全响应头 + CSP nonce + 请求 ID。

响应头在中间件里统一加，页面路由不需要逐个惦记。
CSP 默认用 nonce 模式：每次请求生成随机 nonce，模板里的 <script> 必须带上它才放行，
所以任何通过字符串拼进页面的事件处理器都会失效——这是有意的收紧。
"""

from __future__ import annotations

import time
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings

# 浏览器已知会滥用或已废弃的强权限，一律禁掉
_PERMISSIONS_POLICY = (
    "geolocation=(), camera=(), microphone=(), usb=(), payment=(), interest-cohort=()"
)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": _PERMISSIONS_POLICY,
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Permitted-Cross-Domain-Policies": "none",
}

# 只在 https 下才发的头，本地 http 开发时不会被浏览器搭理
_HSTS = {"Strict-Transport-Security": "max-age=63072000; includeSubDomains"}

def build_csp(nonce: str | None) -> str:
    """按请求拼 CSP。

    带 nonce 时是完整策略，供 HTML 页面使用；不带则收紧到只剩
    frame-ancestors，给静态资源和 API 用——它们本来就不该执行脚本。
    """
    if nonce is None:
        return "frame-ancestors 'none'; base-uri 'none'; sandbox"
    directives = {
        "default-src": "'self'",
        # nonce 模式：只有带本次 nonce 的脚本能跑，拼进页面的事件处理器一律失效
        "script-src": f"'self' 'nonce-{nonce}'",
        # unsafe-inline 仅限 style：图表柱高、二维码尺寸要靠内联样式
        "style-src": "'self' 'unsafe-inline'",
        "img-src": "'self' data:",
        "font-src": "'self'",
        "connect-src": "'self'",
        "form-action": "'self'",
        "frame-ancestors": "'none'",
        "base-uri": "'none'",
        "object-src": "'none'",
        "worker-src": "'self'",
        "manifest-src": "'self'",
    }
    return "; ".join(f"{k} {v}" for k, v in directives.items())


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """给所有响应补安全头，并注入 CSP nonce 与请求 ID。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        nonce = uuid.uuid4().hex
        request.state.csp_nonce = nonce
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id

        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000

        # 静态资源和 API 不需要 nonce，用收紧版策略即可
        is_document = "text/html" in response.headers.get("content-type", "")
        csp = build_csp(nonce if is_document else None)
        is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").startswith("https")
        if csp:
            header = "Content-Security-Policy-Report-Only" if settings.csp_report_only else "Content-Security-Policy"
            response.headers[header] = csp
        for key, value in _SECURITY_HEADERS.items():
            response.headers[key] = value
        if is_https:
            for key, value in _HSTS.items():
                response.headers[key] = value

        response.headers["X-Request-ID"] = request_id
        if settings.debug:
            response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.1f}"
        return response
