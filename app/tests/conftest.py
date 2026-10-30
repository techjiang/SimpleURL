"""测试公共夹具：独立内存库 + TestClient。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
# 后台凭据用 admin:password 那种写法，和安装脚本生成出来的形状一致——
# 只写口令的老写法也还认，但新装的站都是这个形状，测试该跟着真实形态走
os.environ["ADMIN_TOKEN"] = "admin:test-token-please-rotate"
# 测试不联外网：域名一律按「解析到公网」处理，否则断网环境或被墙的域名
# 会让一堆和 DNS 无关的用例随机失败。SSRF 那个用例自己打桩。
os.environ.setdefault("SSRF_RESOLVE_CHECK", "false")


@pytest.fixture(autouse=True)
def _isolate_clicks():
    """每个用例跑完清空延迟计数缓冲与限速器，避免串味。

    限速器是进程级单例，不清的话一个类里连续几次错误登录就把后面
    用例全打成 429，测的就不是业务而是执行顺序了。
    """
    from app.core.clicks import click_buffer
    from app.core.guard import reset_guard

    click_buffer.reset()
    reset_guard()
    # 跳转策略快照有 3 秒 TTL 且是进程级缓存：上一个用例把限速改小、
    # 下一个用例照样读到旧值，测出来的就不是它自己那套配置
    from app.core import policy

    policy.invalidate()
    yield
    click_buffer.reset()
    reset_guard()
    policy.invalidate()


@pytest.fixture()
def client():
    """测试客户端：自动带上 CSRF 令牌，和真实浏览器行为保持一致。

    先 GET 一次首页让中间件把 su_csrf cookie 发下来，后续写请求都从 cookie
    里取值回带到 X-CSRF-Token 头。这样测试覆盖的是同一条校验链路。

    默认按「管理员已登录」起：链接库、导出、统计都只对管理员开放，
    绝大多数用例要验的是业务本身而不是准入。想验访客视角就用 `anon_client`
    或自己发一个不带 cookie 的请求。
    """
    from fastapi.testclient import TestClient

    from app.core import db as db_module
    from app.core.clicks import click_buffer
    from app.main import app

    db_module._engine = None
    db_module._SessionLocal = None
    click_buffer.reset()
    with TestClient(app) as c:
        c.get("/")
        wrapper = _WithCsrf(c)
        r = wrapper.post("/api/admin/login", json={"username": "admin", "password": "test-token-please-rotate"})
        assert r.status_code == 200, f"测试管理员登录失败：{r.text}"
        yield wrapper
    click_buffer.reset()
    db_module._engine = None
    db_module._SessionLocal = None


class _WithCsrf:
    """给 TestClient 的写方法自动补 X-CSRF-Token 头。

    不改测试调用方写法，也不弱化中间件校验——头是从服务端下发的 cookie 里
    原样取出来的，走的还是双提交那条路。
    """

    def __init__(self, inner):
        self._inner = inner

    def _csrf_headers(self, kwargs):
        from app.core.middleware import CSRF_COOKIE, CSRF_HEADER

        headers = dict(kwargs.get("headers") or {})
        # 用例自己指定了 CSRF 头就不插手，否则「故意带错令牌」这类测试永远测不到
        if CSRF_HEADER in {k.lower() for k in headers}:
            return kwargs
        # 逐个翻 cookie jar 取本域的那个：跳转跟到外站后 jar 里会有同名
        # 不同域的 cookie，直接 .get() 会抛 CookieConflict
        token = next(
            (c.value for c in self._inner.cookies.jar if c.name == CSRF_COOKIE and "testserver" in (c.domain or "")),
            "",
        )
        if token:
            headers[CSRF_HEADER] = token
        kwargs["headers"] = headers
        return kwargs

    def post(self, *args, **kwargs):
        return self._inner.post(*args, **self._csrf_headers(kwargs))

    def put(self, *args, **kwargs):
        return self._inner.put(*args, **self._csrf_headers(kwargs))

    def patch(self, *args, **kwargs):
        return self._inner.patch(*args, **self._csrf_headers(kwargs))

    def delete(self, *args, **kwargs):
        return self._inner.delete(*args, **self._csrf_headers(kwargs))

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture()
def anon_client():
    """未登录访客视角：不带任何管理会话，用来验准入边界。"""
    from fastapi.testclient import TestClient

    from app.core import db as db_module
    from app.core.clicks import click_buffer
    from app.main import app

    db_module._engine = None
    db_module._SessionLocal = None
    click_buffer.reset()
    with TestClient(app) as c:
        c.get("/")
        yield _WithCsrf(c)
    click_buffer.reset()
    db_module._engine = None
    db_module._SessionLocal = None


@pytest.fixture()
def flush_clicks():
    """把延迟计数缓冲立即结算，供断言点击数用。"""
    from app.core.clicks import flush_now

    def _run() -> int:
        return flush_now()

    return _run


@pytest.fixture()
def db():
    from sqlalchemy.orm import Session

    from app.core.db import get_sessionmaker, init_db

    init_db()
    sm = get_sessionmaker()
    session = sm()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _restore_process_timezone():
    """守住进程时区。

    有些用例要 `monkeypatch.setenv("TZ", ...)` 再 `time.tzset()` 验证本地时间换算。
    只要有一处写成 `delenv("TZ")`，跑测试的人原本设的 TZ（CI 与容器里就是
    Asia/Shanghai）就被抹掉了，之后凡是跟「今天」「本地零点」有关的用例
    都会按 UTC 算——而且只在跨零点的那几个小时里失败，非常难查。
    这里在每条用例前后各刷一次 tzset，让时区始终回到环境变量说的那个。
    """
    import time as _time

    saved = os.environ.get("TZ")
    _time.tzset()
    yield
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    _time.tzset()
