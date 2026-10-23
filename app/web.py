"""页面渲染（Jinja2）。"""

from __future__ import annotations

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.core.config import settings
from app.core.csrf import current_token
from app.core.timeutil import ensure_aware, fmt_local, iso, local_naive

templates = Jinja2Templates(directory=str(settings.templates_dir))
templates.env.globals.update(site_name=settings.site_name)


def _csrf_token(request) -> str:
    """模板里取 CSRF 令牌用：get_template().render() 不走 Request 上下文也能拿到。"""
    return current_token(request)


def _csp_nonce(request) -> str:
    return getattr(getattr(request, "state", None), "csp_nonce", "") or ""


templates.env.globals.update(csrf_token=_csrf_token, csp_nonce=_csp_nonce)


def _fmt(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return fmt_local(value, fmt)


def _local_input(value) -> str:
    """转成 datetime-local 输入框要的 naive 本地时间串。"""
    naive = local_naive(value)
    return naive.strftime("%Y-%m-%dT%H:%M") if naive else ""


def render_stats(request: Request, link, data: dict) -> str:
    return templates.get_template("stats.html").render(
        request=request,
        link=link,
        data=data,
        short_url=str(request.base_url).rstrip("/") + "/" + link.code,
    )


templates.env.filters.update(fmt=_fmt, local_input=_local_input, iso=iso, aware=ensure_aware)
