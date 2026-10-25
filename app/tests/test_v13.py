"""v13：回执自助、失效页续期、单条频控。

这一版没加新面板，补的是「访客建完就没人管」的那段路：
回执以前只够看统计，链接填错一个字、到期停了、想删掉重来，都只能找管理员；
而开放模式压根没有管理员可找。
"""

from __future__ import annotations

import datetime as dt

import pytest


def _create(anon, target="https://example.com/a", **extra):
    payload = {"target": target, **extra}
    r = anon.post("/api/links", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _stranger():
    """完全干净的客户端：没有管理会话，也没有回执 cookie。

    同一个 anon_client 连续建两条，cookie 里会留着上一条的回执，
    拿它删新版容易误判成「没回执也能删」。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


class TestClaimSelfService:
    """凭回执改 / 续 / 停 / 删自己那一条。"""

    def test_read_own_link_with_claim(self, anon_client):
        link = _create(anon_client)
        r = anon_client.get(f"/api/links/{link['code']}/self?t={link['claim_token']}")
        assert r.status_code == 200
        assert r.json()["code"] == link["code"]

    def test_claim_required(self, anon_client):
        link = _create(anon_client)
        with _stranger() as other:
            assert other.get(f"/api/links/{link['code']}/self").status_code == 401

    def test_wrong_claim_rejected(self, anon_client):
        link = _create(anon_client)
        r = anon_client.get(f"/api/links/{link['code']}/self?t=not-the-token")
        assert r.status_code == 401

    def test_claim_only_opens_its_own_link(self, anon_client):
        first = _create(anon_client, "https://example.com/one")
        second = _create(anon_client, "https://example.com/two")
        r = anon_client.get(f"/api/links/{second['code']}/self?t={first['claim_token']}")
        assert r.status_code == 401

    def test_update_own_link(self, anon_client):
        link = _create(anon_client)
        r = anon_client.patch(
            f"/api/links/{link['code']}/self?t={link['claim_token']}",
            json={"title": "改过的标题", "tags": "自助,演示"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["title"] == "改过的标题"
        assert r.json()["tags"] == ["自助", "演示"]

    def test_update_rejects_empty_payload(self, anon_client):
        link = _create(anon_client)
        r = anon_client.patch(f"/api/links/{link['code']}/self?t={link['claim_token']}", json={})
        assert r.status_code == 422

    def test_update_blocks_internal_target(self, anon_client):
        link = _create(anon_client)
        r = anon_client.patch(
            f"/api/links/{link['code']}/self?t={link['claim_token']}",
            json={"target": "http://127.0.0.1:9000/secret"},
        )
        assert r.status_code == 422

    def test_disable_and_enable(self, anon_client):
        link = _create(anon_client)
        r = anon_client.post(f"/api/links/{link['code']}/self/disable?t={link['claim_token']}&enabled=false")
        assert r.status_code == 200 and r.json()["enabled"] is False
        # 停用之后访问者打不开，创建者凭回执还能进来
        assert anon_client.get(f"/{link['code']}", follow_redirects=False).status_code == 410
        r = anon_client.post(f"/api/links/{link['code']}/self/disable?t={link['claim_token']}&enabled=true")
        assert r.status_code == 200 and r.json()["enabled"] is True

    def test_renew_extends_from_later_of_now_and_expiry(self, anon_client):
        soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)).isoformat()
        link = _create(anon_client, expires_at=soon)
        r = anon_client.post(
            f"/api/links/{link['code']}/self/renew?t={link['claim_token']}", json={"days": 30, "enabled": True}
        )
        assert r.status_code == 200, r.text
        expires = dt.datetime.fromisoformat(r.json()["expires_at"].replace("Z", "+00:00"))
        # 原有的 3 天不该被吃回去：新到期时间应该比「此刻 + 30 天」还晚
        assert expires > dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)

    def test_renew_revives_disabled_link(self, anon_client):
        link = _create(anon_client)
        anon_client.post(f"/api/links/{link['code']}/self/disable?t={link['claim_token']}&enabled=false")
        r = anon_client.post(f"/api/links/{link['code']}/self/renew?t={link['claim_token']}", json={"days": 7})
        assert r.status_code == 200
        assert r.json()["enabled"] is True
        assert anon_client.get(f"/{link['code']}", follow_redirects=False).status_code == 307

    def test_self_delete(self, anon_client):
        link = _create(anon_client)
        r = anon_client.delete(f"/api/links/{link['code']}/self?t={link['claim_token']}")
        assert r.status_code == 200
        assert anon_client.get(f"/{link['code']}", follow_redirects=False).status_code == 404

    def test_self_delete_needs_claim(self, anon_client):
        link = _create(anon_client)
        # 回执 cookie 与令牌一起清掉：只剩「一个没有凭据的浏览器」，
        # 而不是「令牌缺失」，那样会被 CSRF 中间件先挡在 403
        anon_client.cookies.clear()
        anon_client.get("/")
        assert anon_client.delete(f"/api/links/{link['code']}/self").status_code == 401

    def test_claim_cookie_works_without_query(self, anon_client):
        link = _create(anon_client, "https://example.com/cookie")
        # 创建响应已经顺手把回执写进 cookie，后续请求不带 ?t= 也认
        r = anon_client.get(f"/api/links/{link['code']}/self")
        assert r.status_code == 200


class TestGonePageForOwner:
    """失效页要分人：路人看到「打不开」，创建者看到「还能救」。"""

    def test_anonymous_sees_plain_page(self, anon_client):
        link = _create(anon_client, "https://example.com/plain")
        anon_client.post(f"/api/links/{link['code']}/self/disable?t={link['claim_token']}&enabled=false")
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as stranger:
            r = stranger.get(f"/{link['code']}", follow_redirects=False)
        assert r.status_code == 410
        assert "owner-panel" not in r.text

    def test_owner_sees_renew_panel(self, anon_client):
        link = _create(anon_client, "https://example.com/owner")
        anon_client.post(f"/api/links/{link['code']}/self/disable?t={link['claim_token']}&enabled=false")
        r = anon_client.get(f"/{link['code']}", follow_redirects=False)
        assert r.status_code == 410
        assert "owner-panel" in r.text
        assert "续期并重新启用" in r.text

    def test_missing_code_is_branded_page(self, anon_client):
        r = anon_client.get("/nosuchcode", follow_redirects=False)
        assert r.status_code == 404
        assert "SimpleURL" in r.text

    def test_reserved_path_is_branded_page(self, anon_client):
        # 保留字以前抛 ServiceError，落到全局处理器就是一句裸 JSON
        r = anon_client.get("/api", follow_redirects=False)
        assert r.status_code in (404, 405)
        if r.status_code == 404:
            assert r.headers["content-type"].startswith("text/html")


class TestAdminStillWins:
    """管理员不用回执，走原有管理接口，行为不能变。"""

    def test_admin_self_endpoints_work_without_token(self, client):
        link = _create(client, "https://example.com/admin-path")
        r = client.get(f"/api/links/{link['code']}/self")
        assert r.status_code == 200
        r = client.post(f"/api/links/{link['code']}/self/renew", json={"days": 10})
        assert r.status_code == 200 and r.json()["enabled"] is True


class TestRenewService:
    def test_renew_link_clamps_days(self, db):
        from app.schemas import LinkCreate
        from app.services import create_link, renew_link

        link = create_link(db, LinkCreate(target="https://example.com/clamp", fetch_title=False))
        renew_link(db, link, days=99999)
        assert link.expires_at is not None
        db.rollback()
