"""页面渲染（Jinja2）。"""

from __future__ import annotations

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.core import auth
from app.core.config import settings

templates = Jinja2Templates(directory=str(settings.templates_dir))
templates.env.globals.update(site_name=settings.site_name, auth_enabled=auth.admin_enabled)


def render_stats(request: Request, link, data: dict) -> str:
    return templates.get_template("stats.html").render(
        request=request,
        link=link,
        data=data,
        short_url=str(request.base_url).rstrip("/") + "/" + link.code,
    )
