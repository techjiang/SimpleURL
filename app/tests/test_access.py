"""访问模式：开放 / 仅管理员 / 邀请码。"""

from __future__ import annotations

import pytest

from app.core import auth
from app.core.config import settings


@pytest.fixture()
def mode(monkeypatch):
    """切换访问模式，并在用例结束后还原。"""

    def _set(value: str, token: str = "root-secret"):
        monkeypatch.setattr(settings, "access_mode", value)
        monkeypatch.setattr(settings, "admin_token", token)
        auth._login_hits.clear()

    return _set


def login(client, secret: str = "root-secret"):
    return client.post("/api/admin/login", headers={"X-Admin-Token": secret})


def new_link(client, **payload):
    payload.setdefault("target", "https://example.com/page")
    return client.post("/api/links", json=payload)


class TestPublicMode:
    def test_everyone_can_create_but_not_manage(self, client, mode):
        mode("public")
        r = new_link(client, code="pub1")
        assert r.status_code == 201
        # 管理类接口默认锁住
        assert client.get("/api/links").status_code == 401
        assert client.get("/api/links/pub1").status_code == 401
        assert client.get("/api/links/pub1/stats").status_code == 401
        assert client.patch("/api/links/pub1", json={"enabled": False}).status_code == 401
        assert client.delete("/api/links/pub1").status_code == 401

    def test_redirect_and_qr_stay_public(self, client, mode):
        mode("public")
        new_link(client, code="pub2")
        assert client.get("/pub2", follow_redirects=False).status_code == 307
        assert client.get("/api/qr/pub2").status_code == 200

    def test_admin_sees_everything_after_login(self, client, mode):
        mode("public")
        new_link(client, code="pub3")
        assert login(client).status_code == 200
        assert client.get("/api/links").json()["total"] == 1
        assert client.get("/s/pub3").status_code == 200
        assert client.delete("/api/links/pub3").status_code == 200


class TestAdminMode:
    def test_visitor_cannot_create(self, client, mode):
        mode("admin")
        r = new_link(client, code="adm1")
        assert r.status_code == 403
        assert "管理员" in r.json()["message"]

    def test_visitor_sees_mode_and_no_list(self, client, mode):
        mode("admin")
        data = client.get("/api/access").json()
        assert data["mode"] == "admin" and data["is_admin"] is False
        assert data["can_create"] is False
        assert client.get("/api/links").status_code == 401
        assert client.get("/api/stats/summary").json()["total_links"] == 0

    def test_admin_can_create_and_manage(self, client, mode):
        mode("admin")
        assert login(client).status_code == 200
        assert new_link(client, code="adm2").status_code == 201
        assert client.get("/api/links").json()["total"] == 1
        assert client.get("/adm2", follow_redirects=False).status_code == 307
        assert client.get("/api/qr/adm2").status_code == 200

    def test_wrong_secret_rejected(self, client, mode):
        mode("admin")
        assert login(client, "nope").status_code == 401
        assert client.get("/api/access").json()["is_admin"] is False

    def test_logout(self, client, mode):
        mode("admin")
        login(client)
        assert client.post("/api/admin/logout").status_code == 200
        assert client.get("/api/links").status_code == 401

    def test_forged_cookie_rejected(self, client, mode):
        mode("admin")
        client.cookies.set(auth.ADMIN_COOKIE, "1700000000.deadbeef")
        assert client.get("/api/links").status_code == 401

    def test_stats_page_needs_admin(self, client, mode):
        mode("admin")
        login(client)
        new_link(client, code="adm3")
        client.cookies.clear()
        r = client.get("/s/adm3")
        assert r.status_code == 401
        assert "管理员登录" in r.text


class TestInviteMode:
    def test_invalid_invite(self, client, mode):
        mode("invite")
        assert client.post("/api/invite?code=wrong").status_code == 403
        assert new_link(client, code="inv1").status_code == 403

    def test_valid_invite_unlocks_create_only(self, client, mode):
        mode("invite")
        code = auth.invite_key()
        assert client.post(f"/api/invite?code={code}").status_code == 200
        assert new_link(client, code="inv2").status_code == 201
        # 邀请只能创建，管理还是管理员的事
        assert client.get("/api/links").status_code == 401

    def test_admin_sees_invite_code(self, client, mode):
        mode("invite")
        assert client.get("/api/admin/invite").status_code == 401
        login(client)
        data = client.get("/api/admin/invite").json()
        assert data["invite_code"] == auth.invite_key()


class TestFallback:
    def test_admin_mode_without_token_falls_back_to_public(self, client, mode):
        mode("admin", token="")
        data = client.get("/api/access").json()
        assert data["mode"] == "public" and data["can_create"] is True
        assert new_link(client, code="fb1").status_code == 201

    def test_login_disabled_without_token(self, client, mode):
        mode("public", token="")
        assert login(client, "anything").status_code == 400

    def test_legacy_admin_only_flag(self, monkeypatch):
        from app.core.config import Settings

        monkeypatch.setenv("ADMIN_ONLY", "true")
        monkeypatch.delenv("ACCESS_MODE", raising=False)
        assert Settings().access_mode == "admin"

    def test_reserved_codes(self, client, mode):
        mode("public")
        for code in ("admin", "login", "api", "s"):
            assert new_link(client, code=code).status_code == 422
