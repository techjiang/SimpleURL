"""v20：静态资源缓存、二维码空态、分页器位置与键盘可达。

这一轮的问题是「用起来别扭但截图看不出来」的那类：
每个静态资源响应都在种 CSRF cookie 却没有缓存头、二维码工坊没填短码时
甩一块纯白、后台分页器悬在卡片外、顶栏五个链接挡在正文前面。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
ADMIN_HTML = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
ADMIN_JS = (ROOT / "static" / "admin.js").read_text(encoding="utf-8")
BASE_HTML = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")


@pytest.fixture()
def raw_client():
    from app.core import db as db_module
    from app.main import app

    db_module._engine = None
    db_module._SessionLocal = None
    with TestClient(app) as c:
        yield c
    db_module._engine = None
    db_module._SessionLocal = None


class TestStaticCache:
    def test_css_has_cache_control(self, raw_client):
        r = raw_client.get("/static/app.css")
        assert r.status_code == 200
        assert "max-age" in r.headers.get("cache-control", "")

    def test_icons_cache_longer_than_css(self, raw_client):
        css = raw_client.get("/static/app.css").headers["cache-control"]
        icon = raw_client.get("/static/logo/icon.svg").headers["cache-control"]
        assert "max-age=604800" in icon
        assert "max-age=86400" in css

    def test_static_does_not_plant_csrf_cookie(self, raw_client):
        """一张页面十来个资源，每个都往回塞一个 cookie 纯属浪费。"""
        r = raw_client.get("/static/app.css")
        assert "set-cookie" not in {k.lower() for k in r.headers}

    def test_page_still_plants_csrf_cookie(self, raw_client):
        r = raw_client.get("/")
        assert "set-cookie" in {k.lower() for k in r.headers}

    def test_missing_static_is_404_not_html(self, raw_client):
        r = raw_client.get("/static/nope.css")
        assert r.status_code == 404


class TestQrEmptyState:
    def test_preview_starts_empty(self):
        assert 'id="qr-preview"' in ADMIN_HTML
        assert "is-empty" in ADMIN_HTML, "没填短码时不该甩一块白板"

    def test_blank_hint_hidden_when_has_code(self):
        assert re.search(r"\.qr-preview:not\(\.is-empty\)\s+\.qr-blank\s*\{\s*display:\s*none", CSS)

    def test_js_clears_image_on_empty_code(self):
        assert "box.classList.add('is-empty')" in ADMIN_JS
        assert "img.removeAttribute('src')" in ADMIN_JS


class TestPagerPlacement:
    def test_pager_inside_table_wrap(self):
        """分页器要跟表格同一个容器，否则表格一高它就悬在卡片外面。"""
        m = re.search(r'<div class="table-wrap">(.*?)</div>\s*</div>', ADMIN_HTML, re.S)
        assert m, "没找到链接表格的容器"
        assert "admin-page-info" in m.group(1), "分页器应该留在 table-wrap 里"

    def test_pager_row_rule_exists(self):
        assert re.search(r"\.table-wrap \.pager-row\s*\{", CSS)


class TestKeyboardAccess:
    def test_skip_link(self):
        assert 'class="skip-link"' in BASE_HTML
        assert 'href="#main"' in BASE_HTML

    def test_main_is_focus_target(self):
        assert 'id="main"' in BASE_HTML
        assert "tabindex=\"-1\"" in BASE_HTML

    def test_skip_link_rule(self):
        assert re.search(r"\.skip-link:focus[^-]", CSS)
