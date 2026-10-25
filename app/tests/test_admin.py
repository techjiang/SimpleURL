"""管理后台：页面、仪表盘、运行配置、API 密钥、迁移。"""

from __future__ import annotations

import pytest

from app.core import auth, runtime


@pytest.fixture()
def anon():
    """未登录访客。"""
    from fastapi.testclient import TestClient

    from app.core import db as db_module
    from app.core.clickbuffer import click_buffer
    from app.core.config import settings
    from app.main import app

    settings.access_mode = "public"
    settings.admin_token = "test-token"
    runtime.reset()
    db_module._engine = None
    db_module._SessionLocal = None
    with TestClient(app) as c:
        click_buffer.reset()
        yield c
    runtime.reset()


def make(client, code="ad1", **kw):
    payload = {"target": "https://example.com/page", "code": code}
    payload.update(kw)
    r = client.post("/api/links", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


class TestPages:
    def test_admin_redirects_when_anonymous(self, anon):
        r = anon.get("/admin", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/admin/login"

    def test_admin_renders_when_logged_in(self, client):
        r = client.get("/admin")
        assert r.status_code == 200
        assert "管理后台" in r.text

    def test_every_panel_renders(self, client):
        for panel in ("dashboard", "links", "guard", "keys", "migrate", "settings"):
            r = client.get(f"/admin/{panel}")
            assert r.status_code == 200, panel
            assert "管理后台" in r.text

    def test_unknown_panel_falls_back(self, client):
        assert client.get("/admin/nope").status_code == 200

    def test_anonymous_cannot_read_overview(self, anon):
        assert anon.get("/api/admin/overview").status_code == 401
        assert anon.get("/api/admin/settings").status_code == 401
        assert anon.get("/api/admin/keys").status_code == 401


class TestRuntimeSettings:
    def test_save_and_persist(self, client):
        r = client.post("/api/admin/settings", json={"site_name": "短链站"})
        assert r.status_code == 200
        assert runtime.get_str("site_name") == "短链站"
        values = client.get("/api/admin/settings").json()["values"]
        assert values["site_name"] == "短链站"

    def test_invalid_values_are_clamped(self, client):
        client.post("/api/admin/settings", json={"code_length": 999, "guard_ip_per_minute": -5})
        assert runtime.get_int("code_length") == 32  # 上限
        assert runtime.get_int("guard_ip_per_minute") == 0  # 下限

    def test_unknown_key_is_ignored(self, client):
        r = client.post("/api/admin/settings", json={"hack": 1})
        assert r.status_code == 200
        assert "hack" not in runtime.snapshot()

    def test_access_mode_switch_takes_effect(self, client, anon):
        client.post("/api/admin/settings", json={"access_mode": "admin"})
        assert auth.current_mode() == "admin"
        data = anon.get("/api/access").json()
        assert data["mode"] == "admin"
        # 访客不能创建
        assert anon.post("/api/links", json={"target": "https://example.com/x"}).status_code == 403
        client.post("/api/admin/settings", json={"access_mode": "public"})
        assert anon.post("/api/links", json={"target": "https://example.com/y"}).status_code == 201

    def test_mode_keyword_maps_to_three_modes(self, client):
        for mode in ("public", "admin", "invite"):
            client.post("/api/admin/settings", json={"access_mode": mode})
            assert auth.current_mode() == mode


class TestDashboard:
    def test_overview_shape(self, client):
        make(client, "dash1")
        client.get("/dash1")
        data = client.get("/api/admin/overview").json()
        for key in ("total_links", "total_clicks", "daily", "top_links", "queue", "guard"):
            assert key in data
        assert data["total_links"] >= 1
        assert len(data["daily"]) == 14

    def test_days_parameter(self, client):
        data = client.get("/api/admin/overview?days=30").json()
        assert len(data["daily"]) == 30


class TestApiKeys:
    def test_create_list_delete(self, client):
        r = client.post("/api/admin/keys", json={"name": "CI"})
        assert r.status_code == 200
        body = r.json()
        assert body["key"].startswith("sk_")
        key_id = body["item"]["id"]
        listed = client.get("/api/admin/keys").json()["items"]
        assert any(k["id"] == key_id for k in listed)
        # 明文密钥绝不落库展示
        assert all("key" not in k or k.get("key") is None for k in listed)
        assert client.delete(f"/api/admin/keys/{key_id}").status_code == 200
        assert not client.get("/api/admin/keys").json()["items"]

    def test_key_verification(self, client):
        raw = client.post("/api/admin/keys", json={"name": "x"}).json()["key"]
        from app.core.db import get_sessionmaker

        session = get_sessionmaker()()
        try:
            assert auth.verify_api_key(session, raw) is True
            assert auth.verify_api_key(session, "sk_wrong") is False
            assert auth.verify_api_key(session, None) is False
        finally:
            session.close()


class TestMigrate:
    SQL = """INSERT INTO `yourls_url` (`keyword`,`url`,`title`,`timestamp`,`ip`,`clicks`) VALUES
('mk1','https://example.com/one','第一条','2024-01-01 00:00:00','1.1.1.1',3),
('mk2','https://example.com/two','第二条','2024-01-01 00:00:00','1.1.1.1',7);"""

    def test_parse_sql(self):
        from app.admin_service import parse_yourls

        rows = parse_yourls(self.SQL)
        assert len(rows) == 2
        assert rows[0]["keyword"] == "mk1"
        assert rows[0]["clicks"] == 3

    def test_parse_json(self):
        from app.admin_service import parse_yourls

        rows = parse_yourls('[{"keyword":"j1","url":"https://example.com/j","title":"J","clicks":2}]')
        assert rows[0]["keyword"] == "j1"

    def test_import_creates_links(self, client):
        r = client.post("/api/admin/migrate", json={"content": self.SQL})
        assert r.status_code == 200
        data = r.json()
        assert data["created"] == 2
        detail = client.get("/api/links/mk1").json()
        assert detail["clicks"] == 3

    def test_conflict_renames_instead_of_overwriting(self, client):
        make(client, "mk1", target="https://example.com/mine")
        data = client.post("/api/admin/migrate", json={"content": self.SQL}).json()
        assert data["created"] >= 1
        # 原有链接的目标没被覆盖
        assert client.get("/api/links/mk1").json()["target"] == "https://example.com/mine"

    def test_conflict_skip(self, client):
        make(client, "mk1", target="https://example.com/mine")
        data = client.post("/api/admin/migrate", json={"content": self.SQL, "conflict": "skip"}).json()
        assert data["skipped"] == 1

    def test_garbage_input_rejected(self, client):
        assert client.post("/api/admin/migrate", json={"content": "hello"}).status_code == 400


class TestExport:
    def test_export_json(self, client):
        make(client, "ex1")
        items = client.get("/api/admin/export").json()["items"]
        assert any(i["code"] == "ex1" for i in items)


class TestAdminExtras:
    def test_links_panel_filters(self, client):
        make(client, "fx1", target="https://example.com/alpha")
        make(client, "fx2", target="https://example.com/beta")
        r = client.get("/admin/links?q=alpha")
        assert r.status_code == 200
        assert "fx1" in r.text

    def test_links_panel_paging(self, client):
        for i in range(25):
            make(client, f"pg{i:02d}")
        r = client.get("/admin/links?page=2")
        assert r.status_code == 200
        assert "第 2/2 页" in r.text
        # 越界页码要回夹到最后一页，而不是显示「第 99/2 页」
        r = client.get("/admin/links?page=99")
        assert "第 2/2 页" in r.text

    def test_dashboard_range(self, client):
        r = client.get("/admin?days=30")
        assert r.status_code == 200
        assert "近 30 天" in r.text

    def test_clear_clicks_keeps_links(self, client):
        make(client, "dg1")
        client.get("/dg1")
        from app.core.clickbuffer import click_buffer

        click_buffer.flush()
        assert client.post("/api/admin/danger/clear-clicks").status_code == 200
        assert client.get("/api/links/dg1").json()["clicks"] == 0
        assert client.get("/api/links/dg1").status_code == 200

    def test_clear_all_removes_everything(self, client):
        make(client, "dg2")
        assert client.post("/api/admin/danger/clear-all").status_code == 200
        assert client.get("/api/links").json()["total"] == 0

    def test_anonymous_cannot_run_danger(self, anon):
        assert anon.post("/api/admin/danger/clear-all").status_code == 401
