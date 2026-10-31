"""应用入口：装配路由、静态资源、异常处理与生命周期。"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.admin_api import router as admin_router
from app.api import router as api_router
from app.core.clicks import click_buffer
from app.core.config import settings
from app.core.db import init_db
from app.core.middleware import AccessTokenCookieMiddleware, CSRFProtectMiddleware, SecurityHeadersMiddleware
from app.pages import router as pages_router
from app.services import ServiceError
from app.web import render
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
    secret = token.partition(":")[2] if ":" in token else token
    secret = secret or settings.admin_password
    if secret and len(secret) < settings.min_admin_token_length:
        logger.warning(
            "管理口令只有 %d 位，低于建议的 %d 位，建议用 openssl rand -hex 24 生成",
            len(secret),
            settings.min_admin_token_length,
        )
    if not settings.session_secret or settings.session_secret == settings.admin_token:
        logger.warning("SECRET_KEY 未单独配置，正在复用 ADMIN_TOKEN；建议单独设置以支持独立轮换")


def _start_retention_job() -> threading.Timer | None:
    """每天清一次过期点击明细。

    只删明细，不动累计点击。挂在后台线程里，不引 APScheduler 这种重依赖。
    """
    if not settings.retention_days:
        return None
    from app.core.db import session_scope
    from app.services import prune_click_events

    def _run() -> None:
        try:
            with session_scope() as db:
                prune_click_events(db, settings.retention_days)
        except Exception:  # noqa: BLE001 - 清理失败不该影响服务
            logger.exception("清理过期点击明细失败")
        # 回收站也一起收：软删只是「先别真删」，不能变成永远堆着。
        # 不清的话 links 表会一直长，而回收站里那些根本没人会再看。
        try:
            from app.services import purge_expired_trash

            with session_scope() as db:
                purge_expired_trash(db)
        except Exception:  # noqa: BLE001
            logger.exception("清理回收站失败")

    def _tick() -> None:
        _run()
        _reschedule()

    timer: threading.Timer | None = None

    def _reschedule() -> None:
        nonlocal timer
        timer = threading.Timer(24 * 3600, _tick)
        timer.daemon = True
        timer.start()

    _reschedule()
    return timer


def _notify_warnings_once(seen: dict) -> None:
    """把运行状态里的告警推出去，同类告警冷却期内不重复发。

    以前这些黄灯只在后台页面上亮着，得有人主动点开才看得见。
    冷却键用告警文本本身：换了内容就值得再发一次，
    同一句挂一天则发一次就够——告警通道最怕变成噪音。
    """
    import time

    from app.core import notify
    from app.core.db import session_scope
    from app.services import runtime_status

    try:
        with session_scope() as db:
            if not notify.enabled(db):
                return
            from app.core.settings_store import effective

            if not bool(effective(db, "notify_enabled")):
                return
            warnings = runtime_status(db)["warnings"]
            if not warnings:
                return
            now = time.time()
            cooldown = float(effective(db, "notify_cooldown") or 0)
            fresh = []
            for item in warnings:
                key = str(item.get("text") or "")[:200]
                if now - float(seen.get(key, 0) or 0) < cooldown:
                    continue
                fresh.append(item)
            if not fresh:
                return
            # 冷却时间落库之前先占位：发送本身要几秒钟，
            # 两个 tick 撞在一起时会各发一遍
            for item in fresh:
                seen[str(item.get("text") or "")[:200]] = now
            site = str(effective(db, "site_name") or settings.site_name)
            title = f"{site} 有 {len(fresh)} 条告警"
            body = notify.digest_body(fresh, db)
            for channel in notify.enabled(db):
                notify.send(db, channel, title, body, level="warn")
            # 状态留痕（notify.record）写在 db 上，session_scope 退出时会一起提交
    except Exception:  # noqa: BLE001 - 通知失败绝不能影响主进程
        logger.exception("推送告警失败")


def _start_notify_job(interval: int = 300):
    """每 5 分钟看一眼告警，配了通道就推。

    第一轮延迟一个周期：启动瞬间库和配置都还没热，此刻报出来的黄灯
    往往是「刚起来还没读到配置」，推出去只是噪音。
    """
    seen: dict[str, float] = {}

    def _tick() -> None:
        try:
            _notify_warnings_once(seen)
        finally:
            # 发送里抛异常也必须把下一轮排上，否则告警线程死一次就再也不发
            _reschedule()

    timer: threading.Timer | None = None

    def _reschedule() -> None:
        nonlocal timer
        timer = threading.Timer(interval, _tick)
        timer.daemon = True
        timer.start()

    _reschedule()
    return lambda: timer and timer.cancel()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    click_buffer.start(settings.click_flush_interval)
    retention_timer = _start_retention_job()
    stop_notify = _start_notify_job()
    _startup_audit()
    logger.info("SimpleURL v%s 已启动", __version__)
    yield
    if retention_timer is not None:
        retention_timer.cancel()
    stop_notify()
    # 退出前把缓冲里的点击结算掉，别丢数据
    try:
        written = click_buffer.flush_all()
        if written:
            logger.info("退出前结算 %d 条点击", written)
    except Exception:
        logger.exception("退出前结算点击失败")
    click_buffer.stop()
    logger.info("SimpleURL 已关闭")


root_router = APIRouter(include_in_schema=False)


@root_router.api_route("/", methods=["GET", "HEAD"])
def index(request: Request):
    # csrf_token / csp_nonce 由模板全局函数提供；站点名称 / 自述走后端覆盖值
    from app.core.db import session_scope
    from app.core.settings_store import effective

    with session_scope() as db:
        # 公告也走服务端渲染：前端脚本加载前就带上，不会闪一下空白
        announcement = str(effective(db, "announcement") or "").strip()
        html = render("index.html", request, db, announcement=announcement)
    return HTMLResponse(html)


@root_router.api_route("/favicon.ico", methods=["GET", "HEAD"])
def favicon():
    """浏览器默认会打根路径的 /favicon.ico，别再让它落到 404。"""
    path = settings.static_dir / "favicon.ico"
    return FileResponse(path, media_type="image/x-icon", headers={"Cache-Control": "public, max-age=86400"})


@root_router.api_route("/apple-touch-icon-precomposed.png", methods=["GET", "HEAD"])
@root_router.api_route("/apple-touch-icon.png", methods=["GET", "HEAD"])
def apple_touch_icon():
    """iOS 加书签 / 主屏时会自己来找这张图，模板里的 link 它不看。"""
    path = settings.static_dir / "logo" / "icon-180.png"
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


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
app.add_middleware(AccessTokenCookieMiddleware)
app.add_middleware(CSRFProtectMiddleware)
install_yourls_mw(app)
app.include_router(api_router)
app.include_router(admin_router)
app.include_router(yourls_router)
# 站点根级资源（/favicon.ico、/apple-touch-icon.png）必须排在 /{code} 之前：
# 页面路由是通配的，谁先注册谁先吃到请求。
app.include_router(root_router)
app.include_router(pages_router)


@app.exception_handler(ServiceError)
async def service_error_handler(_: Request, exc: ServiceError):
    return JSONResponse(status_code=exc.status, content={"ok": False, "message": exc.message})


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=exc.status_code, content={"ok": False, "message": exc.detail})
    # 静态资源与站点图标 404 就老老实实 404：302 到首页会让浏览器
    # 把一整页 HTML 当 favicon / png 收下，控制台一路报「资源不是图片」
    if exc.status_code == status.HTTP_404_NOT_FOUND and not _is_asset_path(request.url.path):
        return RedirectResponse(url="/", status_code=302)
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "message": exc.detail})


# 这些路径缺什么就返回 404，别拿 HTML 顶替
ASSET_PREFIXES = ("/static/",)
ASSET_SUFFIXES = (
    ".ico", ".png", ".svg", ".webmanifest", ".css", ".js", ".map", ".txt",
    ".woff", ".woff2", ".jpg", ".jpeg", ".webp", ".gif", ".xml",
)


def _is_asset_path(path: str) -> bool:
    return path.startswith(ASSET_PREFIXES) or path.lower().endswith(ASSET_SUFFIXES)


