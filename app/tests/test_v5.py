"""v5：批量操作、操作留痕、创建额度按后台配置生效、跳转策略快照。"""

from __future__ import annotations

import pytest


def _mk(client, code: str, target: str = "https://example.com/a") -> dict:
    r = client.post("/api/links", json={"target": target, "code": code})
    assert r.status_code == 201, r.text
    return r.json()


class TestBulkOps:
    def test_disable_then_enable(self, client):
        for c in ("bk1", "bk2", "bk3"):
            _mk(client, c)
        r = client.post("/api/links/bulk", json={"action": "disable", "codes": ["bk1", "bk2"]})
        assert r.status_code == 200, r.text
        assert r.json()["affected"] == 2
        assert client.get("/bk1", follow_redirects=False).status_code == 410
        assert client.get("/bk3", follow_redirects=False).status_code == 307

        r = client.post("/api/links/bulk", json={"action": "enable", "codes": ["bk1", "bk2"]})
        assert r.json()["affected"] == 2
        assert client.get("/bk1", follow_redirects=False).status_code == 307

    def test_delete(self, client):
        _mk(client, "bd1")
        _mk(client, "bd2")
        r = client.post("/api/links/bulk", json={"action": "delete", "codes": ["bd1", "bd2"]})
        assert r.json()["affected"] == 2
        assert client.get("/api/links/bd1").status_code == 404

    def test_missing_codes_skipped(self, client):
        _mk(client, "bm1")
        r = client.post("/api/links/bulk", json={"action": "disable", "codes": ["bm1", "nope1", "nope2"]})
        body = r.json()
        assert body["affected"] == 1
        assert set(body["missing"]) == {"nope1", "nope2"}

    def test_comma_separated_string_ok(self, client):
        _mk(client, "bc1")
        _mk(client, "bc2")
        r = client.post("/api/links/bulk", json={"action": "disable", "codes": "bc1,bc2"})
        assert r.json()["affected"] == 2

    def test_bad_action_rejected(self, client):
        _mk(client, "ba1")
        r = client.post("/api/links/bulk", json={"action": "explode", "codes": ["ba1"]})
        assert r.status_code == 422

    def test_empty_list_rejected(self, client):
        assert client.post("/api/links/bulk", json={"action": "disable", "codes": []}).status_code == 422

    def test_too_many_rejected(self, client):
        r = client.post("/api/links/bulk", json={"action": "disable", "codes": [f"x{i}" for i in range(501)]})
        assert r.status_code == 422

    def test_anonymous_cannot_bulk(self, client, anon_client):
        _mk(client, "bn1")
        r = anon_client.post("/api/links/bulk", json={"action": "delete", "codes": ["bn1"]})
        assert r.status_code == 401
        assert client.get("/api/links/bn1").status_code == 200

    def test_bulk_delete_clears_claim(self, client):
        """删掉的链接，回执也跟着失效——不能留个还能看统计的入口。"""
        link = _mk(client, "bq1")
        token = link["claim_token"]
        client.post("/api/links/bulk", json={"action": "delete", "codes": ["bq1"]})
        assert client.get(f"/api/links/bq1/stats?t={token}").status_code in (401, 404)


class TestAuditLog:
    def test_create_is_recorded(self, client):
        _mk(client, "au1")
        items = client.get("/api/admin/audit").json()["items"]
        assert any(i["action"] == "create" and i["code"] == "au1" for i in items)

    def test_update_and_delete_recorded(self, client):
        _mk(client, "au2")
        client.patch("/api/links/au2", json={"title": "改个标题"})
        items = client.get("/api/admin/audit?action=update").json()["items"]
        assert any(i["code"] == "au2" and "title" in (i["detail"] or "") for i in items)

        client.delete("/api/links/au2")
        items = client.get("/api/admin/audit?action=delete").json()["items"]
        assert any(i["code"] == "au2" for i in items)

    def test_settings_recorded(self, client):
        client.post("/api/admin/settings", json={"site_name": "改名了"})
        items = client.get("/api/admin/audit?action=settings").json()["items"]
        assert any("site_name" in (i["detail"] or "") for i in items)

    def test_bulk_recorded(self, client):
        _mk(client, "au3")
        _mk(client, "au4")
        client.post("/api/links/bulk", json={"action": "disable", "codes": ["au3", "au4"]})
        items = client.get("/api/admin/audit").json()["items"]
        assert any("批量停用 2 条" in (i["detail"] or "") for i in items)

    def test_actor_is_admin(self, client):
        client.post("/api/admin/settings", json={"code_length": 5})
        items = client.get("/api/admin/audit").json()["items"]
        assert items[0]["actor"] == "admin"

    def test_requires_login(self, anon_client):
        assert anon_client.get("/api/admin/audit").status_code == 401

    def test_limit_respected(self, client):
        for i in range(5):
            _mk(client, f"al{i}")
        assert len(client.get("/api/admin/audit?limit=2").json()["items"]) == 2


class TestCreateQuotaFromSettings:
    def test_admin_setting_takes_effect(self, client):
        """后台改的每日创建上限必须生效，不能只认环境变量。"""
        client.post("/api/admin/settings", json={"create_daily_limit": 2})
        _mk(client, "cq1")
        _mk(client, "cq2")
        r = client.post("/api/links", json={"target": "https://example.com/c", "code": "cq3"})
        assert r.status_code == 429
        assert "上限" in r.json()["message"]

    def test_zero_means_unlimited(self, client):
        client.post("/api/admin/settings", json={"create_daily_limit": 0})
        for i in range(5):
            _mk(client, f"cu{i}")


class TestRedirectPolicySnapshot:
    def test_policy_change_applies_immediately(self, client):
        """快照有 TTL，但后台一保存就必须立刻生效。"""
        _mk(client, "ps1")
        client.post("/api/admin/ua-rules", json={"rules": "curl/"})
        client.get("/ps1", headers={"user-agent": "curl/8.4.0"})  # 先把快照建起来
        client.post("/api/admin/ua-rules", json={"rules": ""})
        # 规则清空后同一个 UA 立刻恢复计数
        assert client.get("/ps1", headers={"user-agent": "curl/8.4.0"}, follow_redirects=False).status_code == 307

    def test_snapshot_shape(self, db):
        from app.core import policy

        policy.invalidate()
        snap = policy.snapshot(db)
        assert {"redirect_rate_limit", "click_daily_limit", "social_guard", "platforms", "ua_rules"} <= set(snap)


class TestRedirectHotPath:
    def test_redirect_still_works(self, client, flush_clicks):
        _mk(client, "hp1")
        for _ in range(3):
            assert client.get("/hp1", follow_redirects=False).status_code == 307
        assert flush_clicks() == 3
        assert client.get("/api/links/hp1").json()["clicks"] == 3

    def test_silent_platform_still_applies(self, client, flush_clicks):
        """策略走快照之后，silent 语义不能丢。"""
        _mk(client, "hp2")
        client.post("/api/admin/social-rules", json={"rules": {"wechat": "silent"}})
        r = client.get("/hp2", headers={"user-agent": "Mozilla/5.0 MicroMessenger/8.0"}, follow_redirects=False)
        assert r.status_code == 307
        flush_clicks()
        assert client.get("/api/links/hp2").json()["clicks"] == 0
