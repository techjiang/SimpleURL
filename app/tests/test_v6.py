"""v6：SSRF 域名解析校验、缓冲上限、根级资源路由、访问令牌门禁、过期提醒、运行状态。"""

from __future__ import annotations

import socket

import pytest

from app.core.config import settings


def _mk(client, code: str, target: str = "https://example.com/a", **payload) -> dict:
    r = client.post("/api/links", json={"target": target, "code": code, **payload})
    assert r.status_code == 201, r.text
    return r.json()


# ---------- SSRF：域名解析 ----------


@pytest.fixture()
def fake_dns(monkeypatch):
    """把指定域名解析到内网，用来验「字面是域名、实际是内网」这一类绕过。"""

    def _install(mapping: dict[str, str]):
        real = socket.getaddrinfo

        def _fake(host, *args, **kwargs):
            if host in mapping:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], 0))]
            return real(host, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", _fake)
        # 解析缓存是进程级的，不清会串味
        from app.core import security

        security._dns_cache.clear()
        monkeypatch.setattr(settings, "ssrf_resolve_check", True)
        return mapping

    yield _install
    from app.core import security

    security._dns_cache.clear()


class TestSsrfResolve:
    def test_domain_pointing_to_loopback_rejected(self, fake_dns):
        fake_dns({"evil.test": "127.0.0.1"})
        from app.core.security import is_public_url

        assert is_public_url("https://evil.test/admin") is False

    def test_domain_pointing_to_metadata_rejected(self, fake_dns):
        fake_dns({"meta.test": "169.254.169.254"})
        from app.core.security import is_public_url

        assert is_public_url("http://meta.test/latest/meta-data/") is False

    def test_api_rejects_domain_resolving_internal(self, fake_dns, client):
        fake_dns({"internal.test": "10.0.0.8"})
        r = client.post("/api/links", json={"target": "https://internal.test/x"})
        assert r.status_code == 422
        assert "合法" in r.json()["message"]

    def test_public_resolution_still_allowed(self, fake_dns):
        fake_dns({"public.test": "93.184.216.34"})
        from app.core.security import is_public_url

        assert is_public_url("https://public.test/page") is True

    def test_title_fetch_skips_internal_domain(self, fake_dns, monkeypatch):
        """抓标题是第二条出网路径，@title 那条规则不能只写在创建校验里。"""
        fake_dns({"internal.test": "127.0.0.1"})
        import httpx

        called = []

        def _boom(*args, **kwargs):
            called.append(args)
            raise RuntimeError("不该发出请求")

        monkeypatch.setattr(httpx.AsyncClient, "get", _boom)

        import asyncio

        from app.core.titles import fetch_title

        assert asyncio.run(fetch_title("http://internal.test/")) is None
        assert called == []

    def test_switch_off_only_checks_literal_ip(self, fake_dns, monkeypatch):
        fake_dns({"internal.test": "10.0.0.8"})
        monkeypatch.setattr(settings, "ssrf_resolve_check", False)
        from app.core.security import is_public_url

        assert is_public_url("https://internal.test/x") is True
        # 字面 IP 那层不受开关影响
        assert is_public_url("http://10.0.0.8/x") is False

    def test_dns_cache_avoid_repeat_lookup(self, fake_dns, monkeypatch):
        from app.core import security

        security._dns_cache.clear()
        calls = []
        real = socket.getaddrinfo

        def _counting(host, *a, **k):
            if host == "cache.test":
                calls.append(host)
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
            return real(host, *a, **k)

        monkeypatch.setattr(socket, "getaddrinfo", _counting)
        monkeypatch.setattr(settings, "ssrf_resolve_check", True)
        security._dns_cache.clear()
        for _ in range(3):
            assert security.is_public_url("https://cache.test/x") is True
        assert len(calls) == 1


# ---------- 延迟计数缓冲上限 ----------


class TestClickBufferCap:
    def test_queue_drops_oldest_when_full(self, monkeypatch):
        from app.core.clicks import click_buffer

        monkeypatch.setattr(settings, "click_buffer_max", 5)
        click_buffer.reset()
        for _ in range(12):
            click_buffer.add(None, 1, None, "curl/8", "1.1.1.1")
        # 上限就是上限：内存不能跟着请求数一起涨
        assert click_buffer.pending() == 5
        assert click_buffer.dropped() == 7
        click_buffer.reset()

    def test_dropped_counter_resets_after_flush(self, monkeypatch, client):
        from app.core.clicks import click_buffer

        monkeypatch.setattr(settings, "click_buffer_max", 2)
        click_buffer.reset()
        _mk(client, "cap1")
        for _ in range(4):
            click_buffer.add(None, 1, None, "curl/8", "1.1.1.1")
        assert click_buffer.dropped() == 2
        click_buffer.flush()
        # 报过就该清零，否则运维看到的是历史累计，永远不知道现在是否还在丢
        assert click_buffer.dropped() == 0
        click_buffer.reset()

    def test_pending_never_exceeds_cap_under_load(self, monkeypatch):
        """狂刷也不该把内存刷爆，这是「高并发不拖垮服务」的底线。"""
        from app.core.clicks import click_buffer

        monkeypatch.setattr(settings, "click_buffer_max", 100)
        click_buffer.reset()
        for _ in range(1000):
            click_buffer.add(None, 7, "https://ref.test", "Mozilla/5.0", "8.8.8.8")
        assert click_buffer.pending() <= 100
        click_buffer.reset()


# ---------- 根级资源路由 ----------


class TestRootAssets:
    @pytest.mark.parametrize("path", ["/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"])
    def test_icon_served(self, client, path):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith(("image/",))

    def test_head_index_ok(self, client):
        # 监控和预取常发 HEAD，405 会被当成站点故障
        assert client.head("/").status_code == 200

    def test_missing_asset_is_404_not_redirect(self, client):
        r = client.get("/static/logo/nope.png", follow_redirects=False)
        assert r.status_code == 404
        assert not r.headers.get("location")

    def test_dotted_path_no_redirect(self, client):
        # sitemap.xml / ads.txt 这类不该被 302 回首页，那看起来像站点抽风
        r = client.get("/sitemap.xml", follow_redirects=False)
        assert r.status_code == 404
        assert not r.headers.get("location")

    def test_head_healthz(self, client):
        assert client.head("/healthz").status_code == 200


# ---------- 访问令牌门禁 ----------


class TestAccessGate:
    @pytest.fixture()
    def gated(self, monkeypatch):
        monkeypatch.setattr(settings, "access_token", "gate-token-123")
        yield "gate-token-123"
        monkeypatch.setattr(settings, "access_token", "")

    def test_token_query_actually_opens_page(self, gated, client):
        """前端门禁靠 ?token= 跳转，服务端不认它的话就是死循环。"""
        _mk(client, "gate1")
        r = client.get("/gate1?token=gate-token-123", follow_redirects=False)
        assert r.status_code == 307

    def test_token_cookie_persists(self, gated, client):
        from app.core.middleware import ACCESS_COOKIE

        _mk(client, "gate2")
        client.get("/gate2?token=gate-token-123", follow_redirects=False)
        assert client.cookies.get(ACCESS_COOKIE) == "gate-token-123"
        # 之后不带令牌也能过
        assert client.get("/gate2", follow_redirects=False).status_code == 307

    def test_wrong_token_still_denied(self, gated, client):
        _mk(client, "gate3")
        assert client.get("/gate3?token=nope", follow_redirects=False).status_code == 403

    def test_access_endpoint_reports_gate(self, gated, anon_client):
        data = anon_client.get("/api/access").json()
        assert data["access_required"] is True
        assert data["access_granted"] is False

    def test_summary_blocked_before_gate(self, gated, anon_client):
        """门口拦人、屋里把概况随便读，这门就是摆设。"""
        assert anon_client.get("/api/stats/summary").status_code == 403
        assert anon_client.get("/api/stats/summary?token=gate-token-123").status_code == 200


# ---------- 过期提醒 ----------


class TestExpiry:
    def _seed(self, client):
        import datetime as dt

        from app.core.timeutil import utcnow

        soon = (utcnow() + dt.timedelta(days=2)).isoformat()
        far = (utcnow() + dt.timedelta(days=90)).isoformat()
        _mk(client, "soon1", expires_at=soon)
        _mk(client, "far1", expires_at=far)
        _mk(client, "never1")

    def test_expiring_filter(self, client):
        self._seed(client)
        r = client.get("/api/links", params={"status": "expiring"})
        codes = [item["code"] for item in r.json()["items"]]
        assert codes == ["soon1"]

    def test_expiring_not_in_expired_filter(self, client):
        self._seed(client)
        r = client.get("/api/links", params={"status": "expired"})
        assert [i["code"] for i in r.json()["items"]] == []

    def test_dashboard_lists_expiring(self, client):
        self._seed(client)
        d = client.get("/api/admin/dashboard").json()
        assert d["expiring_links"] == 1
        assert d["expiring"][0]["code"] == "soon1"

    def test_bad_status_rejected(self, client):
        assert client.get("/api/links", params={"status": "whatever"}).status_code == 422


# ---------- 运行状态 / 清理 ----------


class TestRuntime:
    def test_runtime_payload(self, client):
        _mk(client, "rt1")
        data = client.get("/api/admin/runtime").json()
        assert data["links"] >= 1
        assert data["retention_days"] >= 1
        assert isinstance(data["warnings"], list)
        assert "dropped_clicks" in data

    def test_runtime_requires_admin(self, anon_client):
        assert anon_client.get("/api/admin/runtime").status_code == 401

    def test_prune_removes_only_old_events(self, client, db):
        import datetime as dt

        from app.core.timeutil import utcnow
        from app.models import ClickEvent, Link
        from app.services import prune_click_events

        link = _mk(client, "pr1")
        row = db.query(Link).filter_by(code="pr1").one()
        db.add_all(
            [
                ClickEvent(link_id=row.id, ts=utcnow() - dt.timedelta(days=400)),
                ClickEvent(link_id=row.id, ts=utcnow()),
            ]
        )
        db.flush()
        deleted = prune_click_events(db, 365)
        db.commit()
        assert deleted == 1
        # 累计点击数不受清理影响
        db.expire_all()
        assert db.query(Link).filter_by(code="pr1").one().clicks == 0
        assert link["code"] == "pr1"

    def test_prune_endpoint_requires_admin(self, anon_client):
        assert anon_client.post("/api/admin/prune-events", json={}).status_code == 401

    def test_healthz_reports_queue(self, client, monkeypatch):
        from app.core.clicks import click_buffer

        click_buffer.reset()
        data = client.get("/healthz").json()
        assert data["status"] == "ok"
        assert data["pending_clicks"] == 0
        click_buffer.reset()


# ---------- 弱口令 ----------


class TestWeakToken:
    def test_weak_token_locks_guest_creation(self, monkeypatch, anon_client, client):
        monkeypatch.setattr(settings, "admin_token", "123456")
        r = anon_client.post("/api/links", json={"target": "https://example.com/x"})
        assert r.status_code == 403
        assert "口令" in r.json()["message"]

    def test_weak_token_reported_in_snapshot(self, monkeypatch, anon_client):
        monkeypatch.setattr(settings, "admin_token", "123456")
        data = anon_client.get("/api/access").json()
        assert data["locked"] is True
        assert any("ADMIN_TOKEN" in w["text"] for w in data["warnings"])

    def test_admin_creation_still_works(self, monkeypatch, client):
        """口令弱要收权，但不能把管理员自己也锁在门外。"""
        monkeypatch.setattr(settings, "admin_token", "123456")
        r = client.post("/api/links", json={"target": "https://example.com/x"})
        assert r.status_code == 201

    def test_strong_token_not_locked(self, client):
        assert client.get("/api/access").json()["locked"] is False


# ---------- 站点公告 ----------


class TestAnnouncement:
    def test_empty_by_default(self, client):
        assert client.get("/api/access").json()["announcement"] == ""

    def test_save_and_read(self, client):
        assert client.post("/api/admin/settings", json={"announcement": "周六维护"}).status_code == 200
        assert client.get("/api/access").json()["announcement"] == "周六维护"
        assert client.get("/api/admin/settings").json()["announcement"] == "周六维护"

    def test_can_be_cleared(self, client):
        client.post("/api/admin/settings", json={"announcement": "周六维护"})
        client.post("/api/admin/settings", json={"announcement": ""})
        assert client.get("/api/access").json()["announcement"] == ""

    def test_only_admin_can_set(self, anon_client):
        assert anon_client.post("/api/admin/settings", json={"announcement": "x"}).status_code == 401
