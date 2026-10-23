"""应用入口：装配路由、静态资源、异常处理与生命周期。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.admin_api import router as admin_router
from app.api import router as api_router
from app.core.clicks import click_buffer
from app.core.config import settings
from app.core.db import init_db
from app.core.middleware import CSRFProtectMiddleware, SecurityHeadersMiddleware
from app.pages import router as pages_router
from app.services import ServiceError
from app.web import templates
from app.yourls_api import install as install_yourls_mw
from app.yourls_api import router as yourls_router

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("simpleurl")


def _startup_audit() -> None:
    """启动自检：把一眼可见的安全隐患喊出来，别等出事才发现。"""
    token = settings.admin_token
    if token and len(token) < settings.min_admin_token_length:
        logger.warning(
            "ADMIN_TOKEN 只有 %d 位，低于建议的 %d 位，建议用 openssl rand -hex 24 生成",
            len(token),
            settings.min_admin_token_length,
        )
    if not settings.session_secret or settings.session_secret == settings.admin_token:
        logger.warning("SECRET_KEY 未单独配置，正在复用 ADMIN_TOKEN；建议单独设置以支持独立轮换")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    click_buffer.start(settings.click_flush_interval)
    _startup_audit()
    logger.info("SimpleURL v%s 已启动", __version__)
    yield
    # 退出前把缓冲里的点击结算掉，别丢数据
    try:
        written = click_buffer.flush_all()
        if written:
            logger.info("退出前结算 %d 条点击", written)
    except Exception:
        logger.exception("退出前结算点击失败")
    click_buffer.stop()
    logger.info("SimpleURL 已关闭")


app = FastAPI(
    title=f"{settings.site_name} API",
    description="轻量、流畅、好用的短链接服务",
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs" if settings.debug else None,
    redoc_url=None,
)

app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
# 安全中间件：外层先补响应头并签发 CSRF 令牌，内层再校验写请求
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CSRFProtectMiddleware)
install_yourls_mw(app)
app.include_router(api_router)
app.include_router(admin_router)
app.include_router(yourls_router)
app.include_router(pages_router)


@app.exception_handler(ServiceError)
async def service_error_handler(_: Request, exc: ServiceError):
    return JSONResponse(status_code=exc.status, content={"ok": False, "message": exc.message})


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=exc.status_code, content={"ok": False, "message": exc.detail})
    # 页面 404 回首页
    if exc.status_code == status.HTTP_404_NOT_FOUND:
        return RedirectResponse(url="/", status_code=302)
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "message": exc.detail})


@app.get("/", include_in_schema=False)
def index(request: Request):
    # csrf_token / csp_nonce 由模板全局函数提供，这里不用显式传
    return templates.TemplateResponse(request, "index.html")
