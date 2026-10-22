"""应用入口：装配路由、静态资源、异常处理与生命周期。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api import router as api_router
from app.core.config import settings
from app.core.db import init_db
from app.pages import router as pages_router
from app.services import ServiceError
from app.web import templates

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("simpleurl")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    logger.info("SimpleURL v%s 已启动", __version__)
    yield
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
app.include_router(api_router)
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
    return templates.TemplateResponse(request, "index.html")
