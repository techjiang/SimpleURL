"""v7：标题真的会去取、中文站点的标签筛选、外站能引用站内资源。

这一轮的重点不是加功能，而是把「界面上有、实际没接线」的地方接上。
每条用例都对应一个能复现的具体现象。
"""

from __future__ import annotations

import asyncio

import pytest


class TestTitleFetch:
    def test_fetch_runs_inside_request_thread(self, client, monkeypatch):
        """创建接口是同步路由，标题抓取必须能在「事件循环已在跑」时工作。

        原来 `create_link` 里只有 `guess_title`，从没发过请求——
        页面上那个开关拨来拨去都不影响结果。现在把网络层打桩，
        断言确实去取了、并且用的是抓回来的值。
        """
        from app.core import titles

        called: list[str] = []

        async def fake_fetch(url: str, timeout: int = 5):
            called.append(url)
            return "真实的页面标题"

        monkeypatch.setattr(titles, "fetch_title", fake_fetch)
        r = client.post("/api/links", json={"target": "https://example.com/some/page"})
        assert r.status_code == 201
        body = r.json()
        assert called == ["https://example.com/some/page"]
        assert body["title"] == "真实的页面标题"
        assert body["title_source"] == "fetched"

    def test_fallback_marks_source(self, client, monkeypatch):
        """抓不到就兜底，但要标成 fallback，别让人以为抓成功了。"""
        from app.core import titles

        async def fake_fetch(url: str, timeout: int = 5):
            return None

        monkeypatch.setattr(titles, "fetch_title", fake_fetch)
        r = client.post("/api/links", json={"target": "https://example.com/a/long-slug"})
        body = r.json()
        assert body["title"] == "long slug"
        assert body["title_source"] == "fallback"

    def test_switch_off_skips_network(self, client, monkeypatch):
        from app.core import titles

        called: list[str] = []

        async def fake_fetch(url: str, timeout: int = 5):
            called.append(url)
            return "不该被取到"

        monkeypatch.setattr(titles, "fetch_title", fake_fetch)
        client.post("/api/admin/settings", json={"fetch_title": False})
        r = client.post("/api/links", json={"target": "https://example.com/x"})
        assert r.json()["title"] is None
        assert called == []

    def test_manual_title_wins_and_marked(self, client, monkeypatch):
        from app.core import titles

        async def fake_fetch(url: str, timeout: int = 5):
            return "抓回来的标题"

        monkeypatch.setattr(titles, "fetch_title", fake_fetch)
        r = client.post("/api/links", json={"target": "https://example.com/x", "title": "我自己写的"})
        body = r.json()
        assert body["title"] == "我自己写的"
        assert body["title_source"] == "manual"

    def test_per_request_switch_off(self, client, monkeypatch):
        """导入脚本常常不想等出网：单次请求就能关掉。"""
        from app.core import titles

        called: list[str] = []

        async def fake_fetch(url: str, timeout: int = 5):
            called.append(url)
            return "不该被取到"

        monkeypatch.setattr(titles, "fetch_title", fake_fetch)
        r = client.post("/api/links", json={"target": "https://example.com/y", "fetch_title": False})
        assert r.status_code == 201
        assert called == []

    def test_redirect_to_internal_is_refused(self, monkeypatch):
        """公网地址 302 到内网元数据地址，是绕 SSRF 的老路子。"""
        from app.core import titles

        hops: list[str] = []

        class FakeResponse:
            def __init__(self, status_code, headers=None, content=b""):
                self.status_code = status_code
                self.headers = headers or {}
                self.content = content

            @property
            def charset_encoding(self):
                return "utf-8"

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None):
                hops.append(url)
                if len(hops) == 1:
                    return FakeResponse(302, {"location": "http://169.254.169.254/latest/meta-data"})
                return FakeResponse(200, {}, b"<title>internal</title>")

        monkeypatch.setattr(titles.httpx, "AsyncClient", FakeClient)
        # 第一跳是公网，允许；第二跳落到链路本地，必须停手
        monkeypatch.setattr(titles, "_is_safe_host", lambda url: "169.254.169.254" not in url)
        assert asyncio.run(titles.fetch_title("https://example.com/")) is None
        assert len(hops) == 1

    def test_title_is_parsed_from_html(self, monkeypatch):
        from app.core import titles

        class FakeResponse:
            status_code = 200
            headers: dict = {}
            content = b"<html><head><title>\xe4\xb8\xad\xe6\x96\x87\xe6\xa0\x87\xe9\xa2\x98</title></head></html>"

            @property
            def charset_encoding(self):
                return "utf-8"

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None):
                return FakeResponse()

        monkeypatch.setattr(titles.httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(titles, "_is_safe_host", lambda url: True)
        assert asyncio.run(titles.fetch_title("https://example.com/")) == "中文标题"


class TestCrossOriginAssets:
    def test_static_is_loadable_from_other_sites(self, client):
        """二维码 / 图标贴到外站时不能被 CORP 挡掉。"""
        r = client.get("/static/logo/icon.svg")
        assert r.headers["cross-origin-resource-policy"] == "cross-origin"

    def test_pages_stay_locked_down(self, client):
        r = client.get("/")
        assert r.headers["cross-origin-resource-policy"] == "same-origin"
        assert r.headers["x-frame-options"] == "DENY"

    def test_qr_image_embeddable(self, client):
        client.post("/api/links", json={"target": "https://example.com/qr", "code": "qrx1"})
        r = client.get("/api/qr/qrx1")
        assert r.status_code == 200
        assert r.headers["cross-origin-resource-policy"] == "cross-origin"

    def test_can_be_turned_off(self, client, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "static_cross_origin", False)
        r = client.get("/static/logo/icon.svg")
        assert r.headers["cross-origin-resource-policy"] == "same-origin"


class TestTagFilter:
    def _make(self, client):
        client.post("/api/links", json={"target": "https://example.com/1", "tags": ["活动", "文档"]})
        client.post("/api/links", json={"target": "https://example.com/2", "tags": ["活动"]})
        client.post("/api/links", json={"target": "https://example.com/3"})

    def test_tag_list_counts(self, client):
        self._make(client)
        items = client.get("/api/tags").json()["items"]
        by_name = {i["tag"]: i["count"] for i in items}
        assert by_name == {"活动": 2, "文档": 1}

    def test_filter_by_tag(self, client):
        self._make(client)
        r = client.get("/api/links", params={"tag": "活动"})
        assert r.json()["total"] == 2

    def test_anonymous_can_read_tags(self, anon_client):
        """标签本身不含隐私，访客也读得到（否则前台下拉是空的）。"""
        r = anon_client.get("/api/tags")
        assert r.status_code == 200


class TestBulkTolerance:
    def test_leading_slash_and_full_url(self, client):
        """界面上显示的是 `/abc`，勾选后原样提交不该静默失效。"""
        client.post("/api/links", json={"target": "https://example.com/a", "code": "tga1"})
        client.post("/api/links", json={"target": "https://example.com/b", "code": "tgb1"})
        r = client.post(
            "/api/links/bulk",
            json={"action": "disable", "codes": ["/tga1", "http://testserver/tgb1"]},
        )
        body = r.json()
        assert body["affected"] == 2
        assert body["missing"] == []


class TestSilentIsNotBlocked:
    def test_silent_counted_separately(self, client, flush_clicks):
        """「放行不计数」为用户其实打开了页面，别在统计页报成被拦。"""
        client.post("/api/links", json={"target": "https://example.com/s", "code": "sil1"})
        client.post("/api/admin/social-rules", json={"rules": {"wechat": "silent"}})
        ua = "Mozilla/5.0 (iPhone) MicroMessenger/8.0"
        r = client.get("/sil1", headers={"user-agent": ua}, follow_redirects=False)
        assert r.status_code == 307  # 照常跳转
        flush_clicks()
        stats = client.get("/api/links/sil1/stats").json()
        assert stats["clicks"] == 0
        assert stats["blocked"] == 1
        assert stats["silent"] == 1


class TestBrandingTakesEffect:
    def test_site_name_override_shows_up(self, client):
        """后台改了站点名称，页面标题与页脚要立刻跟着变。"""
        client.post("/api/admin/settings", json={"site_name": "短链小站", "site_tagline": "自用短链"})
        r = client.get("/")
        assert "<title>短链小站" in r.text
        assert "短链小站" in r.text
        assert "自用短链" in r.text

    def test_tagline_roundtrip(self, client):
        client.post("/api/admin/settings", json={"site_tagline": "自己家的短链"})
        assert client.get("/api/admin/settings").json()["site_tagline"] == "自己家的短链"


class TestRuntimeTitleAudit:
    def test_fallback_only_triggers_warning(self, client, monkeypatch):
        from app.core import titles

        async def fake_fetch(url: str, timeout: int = 5):
            return None

        monkeypatch.setattr(titles, "fetch_title", fake_fetch)
        for i in range(3):
            client.post("/api/links", json={"target": f"https://example.com/p{i}"})
        status = client.get("/api/admin/runtime").json()
        assert status["title_sources"]["fallback"] == 3
        assert any("自动取标题" in w["text"] for w in status["warnings"])

    def test_success_counts(self, client, monkeypatch):
        from app.core import titles

        async def fake_fetch(url: str, timeout: int = 5):
            return "标题"

        monkeypatch.setattr(titles, "fetch_title", fake_fetch)
        client.post("/api/links", json={"target": "https://example.com/ok"})
        status = client.get("/api/admin/runtime").json()
        assert status["title_sources"]["fetched"] == 1
        assert not any("自动取标题" in w["text"] for w in status["warnings"])


@pytest.mark.parametrize(
    "raw,expect",
    [("/abc", "abc"), ("https://s.example.com/abc", "abc"), ("  abc ", "abc"), ("abc", "abc")],
)
def test_bulk_code_normalize(raw, expect):
    from app.services import _bulk_code

    assert _bulk_code(raw) == expect
