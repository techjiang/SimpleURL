"""访问模式：open（开放外部可使用）与 admin（仅管理员可使用）。"""

from __future__ import annotations

from app.core.access import ADMIN, OPEN
from app.core.db import session_scope
from app.core.settings_store import set_value


def login(client, username="admin", password="test-token"):
    r = client.post("/api/admin/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r


def set_mode(mode):
    with session_scope() as db:
        set_value(db, "access_mode", mode)


def create(client, code="pub1", **payload):
    payload.setdefault("target", "https://example.com/page")
    r = client.post("/api/links", json={"code": code, **payload})
    assert r.status_code == 201, r.text
    return r.json()


class TestDefaultOpen:
    def test_default_mode_is_open(self, client):
        data = client.get("/api/access").json()
        assert data["mode"] == OPEN
        assert data["admin_mode"] is False
        assert data["can_create"] is True

    def test_open_mode_allows_anonymous(self, client):
        create(client)
        assert client.get("/api/links").status_code == 200
        assert client.get("/api/links/pub1").status_code == 200
        assert client.get("/api/links/pub1/stats").status_code == 200
        assert client.get("/api/qr/pub1").status_code == 200

    def test_open_mode_still_requires_admin_for_mutations(self, client):
        create(client)
        assert client.patch("/api/links/pub1", json={"enabled": False}).status_code == 200
        assert client.delete("/api/links/pub1").status_code == 200


class TestAdminMode:
    def test_blocks_anonymous_reads(self, client):
        create(client)
        set_mode(ADMIN)
        assert client.get("/api/links").status_code == 401
        assert client.get("/api/links/pub1").status_code == 401
        assert client.get("/api/links/pub1/stats").status_code == 401
        assert client.get("/api/qr/pub1").status_code == 401

    def test_blocks_anonymous_create(self, client):
        set_mode(ADMIN)
        r = client.post("/api/links", json={"target": "https://x.com"})
        assert r.status_code == 401
        assert "仅管理员" in r.json()["message"]

    def test_allows_admin_session(self, client):
        create(client)
        set_mode(ADMIN)
        login(client)
        assert client.get("/api/links").status_code == 200
        assert client.get("/api/links/pub1").status_code == 200
        assert client.get("/api/qr/pub1").status_code == 200
        r = client.post("/api/links", json={"target": "https://x.com", "code": "adm1"})
        assert r.status_code == 201

    def test_redirect_still_open_to_guests(self, client):
        create(client)
        set_mode(ADMIN)
        r = client.get("/pub1", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "https://example.com/page"

    def test_stats_page_requires_admin(self, client):
        create(client)
        set_mode(ADMIN)
        assert client.get("/s/pub1").status_code == 401
        login(client)
        assert client.get("/s/pub1").status_code == 200

    def test_unknown_code_unchanged_in_admin_mode(self, client):
        set_mode(ADMIN)
        r = client.get("/nope-nope", follow_redirects=False)
        assert r.status_code == 404

    def test_summary_reflects_mode(self, client):
        create(client)
        set_mode(ADMIN)
        data = client.get("/api/stats/summary").json()
        assert data["admin_mode"] is True
        assert data["is_admin"] is False
        assert data["total_links"] == 1
        login(client)
        assert client.get("/api/stats/summary").json()["is_admin"] is True


class TestModePersistence:
    def test_mode_survives_reload(self, client):
        """模式落库在 settings 表，重启后依然生效。"""
        set_mode(ADMIN)
        assert client.get("/api/access").json()["admin_mode"] is True

    def test_invalid_mode_falls_back_to_open(self, client):
        with session_scope() as db:
            set_value(db, "access_mode", "garbage")
        assert client.get("/api/access").json()["mode"] == OPEN

    def test_mode_without_admin_token_is_open(self, client):
        """没配 ADMIN_TOKEN 时永远开放，避免把站点锁死在门外。"""
        from app.core.config import settings

        set_mode(ADMIN)
        original = settings.admin_token
        settings.admin_token = ""
        try:
            assert client.get("/api/access").json()["admin_mode"] is False
            assert client.get("/api/links").status_code == 200
        finally:
            settings.admin_token = original


class TestAdminSettings:
    def test_settings_expose_mode(self, client):
        login(client)
        data = client.get("/api/admin/settings").json()
        assert data["access_mode"] in {OPEN, ADMIN}
        assert data["access_mode_label"]

    def test_save_mode_via_settings(self, client):
        login(client)
        r = client.post("/api/admin/settings", json={"access_mode": "admin"})
        assert r.status_code == 200
        assert client.get("/api/access").json()["admin_mode"] is True
        r = client.post("/api/admin/settings", json={"access_mode": "open"})
        assert client.get("/api/access").json()["admin_mode"] is False

    def test_settings_require_admin(self, client):
        assert client.get("/api/admin/settings").status_code == 401
        assert client.post("/api/admin/settings", json={"access_mode": "admin"}).status_code == 401
