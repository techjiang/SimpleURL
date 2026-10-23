"""CSRF 双提交令牌的读取助手。

令牌本身在 app.core.middleware 里生成并写进 cookie，这里只提供
「当前请求该往请求头里放什么值」的读取入口，前端通过页面模板拿到。
"""

from __future__ import annotations

from starlette.requests import Request

from app.core.middleware import CSRF_COOKIE, CSRF_HEADER, issue_token


def current_token(request: Request) -> str:
    """当前请求的 CSRF 令牌；没有就先发一个新的（中间件会顺手写 cookie）。"""
    return getattr(request.state, "csrf_token", None) or request.cookies.get(CSRF_COOKIE) or issue_token()


__all__ = ["CSRF_COOKIE", "CSRF_HEADER", "current_token"]
