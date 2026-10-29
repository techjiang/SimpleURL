"""页面渲染（Jinja2）。"""

from __future__ import annotations

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.core import auth
from app.core.config import settings
from app.core.timeutil import ensure_aware

templates = Jinja2Templates(directory=str(settings.templates_dir))


def _dt(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """模板里的时间一律走这里：SQLite 读回的是 naive，直接 strftime 会把 UTC 当本地时间。"""
    aware = ensure_aware(value)
    return aware.strftime(fmt) if aware else ""


templates.env.filters["dt"] = _dt

# 署名是写死的，不走 runtime、不走环境变量、后台设置里也删不掉。
# 想改只能改代码，这样「Powered by SimpleURL」才算是真的永久锁定。
ATTRIBUTION_TEXT = "Powered by SimpleURL"
ATTRIBUTION_URL = "https://github.com/techjiang/SimpleURL"

templates.env.globals.update(
    site_name=settings.site_name,
    auth_enabled=auth.admin_enabled(),
    attribution_text=ATTRIBUTION_TEXT,
    attribution_url=ATTRIBUTION_URL,
)


def csp_nonce(request) -> str:
    """取本次请求的 CSP nonce。

    nonce 由安全中间件写在 request.state 上。CSP 用 nonce 模式后，
    只有带上匹配 nonce 的 <script> 才会执行，拼进页面的内联事件处理器一律失效。
    """
    return getattr(getattr(request, "state", None), "csp_nonce", "") or ""


def render(request: Request, template_name: str, **context) -> str:
    """统一的模板渲染入口：自动注入 CSP nonce。"""
    return templates.get_template(template_name).render(
        request=request, csp_nonce=csp_nonce(request), **context
    )


def render_stats(request: Request, link, data: dict) -> str:
    return render(
        request,
        "stats.html",
        link=link,
        data=data,
        short_url=str(request.base_url).rstrip("/") + "/" + link.code,
    )
