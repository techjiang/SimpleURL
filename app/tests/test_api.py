"""API 端到端测试。"""

from __future__ import annotations

from sqlalchemy import select


def create(client, **payload):
    payload.setdefault("target", "https://example.com/page")
    r = client.post("/api/links", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


class TestCreate:
    def test_auto_code(self, client):
        link = create(client)
        assert 3 <= len(link["code"]) <= 16
        assert link["target"] == "https://example.com/page"
        assert link["short_url"].endswith("/" + link["code"])
        assert link["clicks"] == 0 and link["enabled"] is True

    def test_url_normalize(self, client):
        assert create(client, target="example.com/x")["target"] == "https://example.com/x"

    def test_custom_code(self, client):
        link = create(client, code="my-link", tags=["a", "b"])
        assert link["code"] == "my-link"
        assert link["tags"] == ["a", "b"]

    def test_duplicate_code(self, client):
        create(client, code="dup")
        r = client.post("/api/links", json={"target": "https://x.com", "code": "dup"})
        assert r.status_code == 409
        assert "占用" in r.json()["message"]

    def test_code_case_insensitive_conflict(self, client):
        create(client, code="mixed")
        r = client.post("/api/links", json={"target": "https://x.com", "code": "MIXED"})
        assert r.status_code == 409

    def test_invalid_scheme(self, client):
        r = client.post("/api/links", json={"target": "javascript:alert(1)"})
        assert r.status_code == 422

    def test_localhost_blocked(self, client):
        r = client.post("/api/links", json={"target": "http://127.0.0.1:8080/admin"})
        assert r.status_code == 422

    def test_empty_target(self, client):
        r = client.post("/api/links", json={"target": "  "})
        assert r.status_code == 422

    def test_reserved_code(self, client):
        r = client.post("/api/links", json={"target": "https://x.com", "code": "api"})
        assert r.status_code == 422

    def test_code_too_long(self, client):
        r = client.post("/api/links", json={"target": "https://x.com", "code": "a" * 17})
        assert r.status_code == 422

    def test_past_expiry_rejected(self, client):
        r = client.post(
            "/api/links", json={"target": "https://x.com", "expires_at": "2000-01-01T00:00:00Z"}
        )
        assert r.status_code == 422

    def test_tags_dedup_and_cap(self, client):
        link = create(client, tags=["a", "A", "b"] + [f"t{i}" for i in range(20)])
        assert len(link["tags"]) <= 10
        assert len({t.lower() for t in link["tags"]}) == len(link["tags"])

    def test_missing_body(self, client):
        assert client.post("/api/links", json={}).status_code == 422


class TestRedirect:
    def test_redirect_and_click(self, client):
        from app.core.clickbuffer import click_buffer

        create(client, code="go1")
        r = client.get("/go1", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "https://example.com/page"
        click_buffer.flush()  # 延迟计数：读点击数前先结算
        detail = client.get("/api/links/go1").json()
        assert detail["clicks"] == 1

    def test_case_insensitive_lookup(self, client):
        create(client, code="Case1")
        assert client.get("/case1", follow_redirects=False).status_code == 307

    def test_unknown_code(self, client):
        r = client.get("/does-not-exist", follow_redirects=False)
        assert r.status_code == 404
        assert "不存在" in r.text

    def test_disabled_link(self, client):
        create(client, code="off")
        client.patch("/api/links/off", json={"enabled": False})
        r = client.get("/off", follow_redirects=False)
        assert r.status_code == 410
        assert "停用" in r.text

    def test_expired_link_blocks_redirect(self, client):
        """过期时间到点后必须拦截跳转（用直接改库模拟时间流逝）。"""
        from datetime import UTC, datetime, timedelta

        from app.core.db import session_scope
        from app.models import Link

        create(client, code="exp", expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
        with session_scope() as db:
            link = db.execute(select(Link).where(Link.code == "exp")).scalar_one()
            link.expires_at = datetime.now(UTC) - timedelta(minutes=1)

        r = client.get("/exp", follow_redirects=False)
        assert r.status_code == 410
        assert "过期" in r.text
        assert client.get("/api/links?status=expired").json()["total"] == 1

    def test_static_path_not_treated_as_code(self, client):
        assert client.get("/static/app.css").status_code == 200


class TestList:
    def test_pagination(self, client):
        for _ in range(5):
            create(client)
        data = client.get("/api/links?page=1&page_size=2").json()
        assert data["total"] == 5 and len(data["items"]) == 2

    def test_search(self, client):
        create(client, code="aaa", target="https://alpha.com/1", title="Alpha 文档")
        create(client, code="bbb", target="https://beta.com/2")
        assert client.get("/api/links?q=alpha").json()["total"] == 1
        assert client.get("/api/links?q=文档").json()["total"] == 1

    def test_status_filter(self, client):
        create(client, code="on1")
        create(client, code="off1")
        client.patch("/api/links/off1", json={"enabled": False})
        assert client.get("/api/links?status=disabled").json()["total"] == 1

    def test_sort_by_clicks(self, client):
        create(client, code="low")
        create(client, code="high")
        client.get("/high")
        client.get("/high")
        items = client.get("/api/links?sort=clicks_desc").json()["items"]
        assert items[0]["code"] == "high"

    def test_bad_sort(self, client):
        assert client.get("/api/links?sort=nope").status_code == 422


class TestUpdateDelete:
    def test_update(self, client):
        create(client, code="upd")
        r = client.patch("/api/links/upd", json={"title": "新标题", "tags": ["x"]})
        assert r.status_code == 200
        assert r.json()["title"] == "新标题" and r.json()["tags"] == ["x"]

    def test_code_immutable(self, client):
        create(client, code="keep")
        r = client.patch("/api/links/keep", json={"code": "other"})
        assert r.json()["code"] == "keep"

    def test_delete(self, client):
        create(client, code="del")
        assert client.delete("/api/links/del").status_code == 200
        assert client.get("/api/links/del").status_code == 404

    def test_delete_missing(self, client):
        assert client.delete("/api/links/ghost").status_code == 404


class TestStatsAndQr:
    def test_stats(self, client):
        from app.core.clickbuffer import click_buffer

        create(client, code="st1")
        client.get("/st1")
        client.get("/st1")
        click_buffer.flush()  # 点击是攒批落库的，读统计前先结算
        data = client.get("/api/links/st1/stats").json()
        assert data["clicks"] == 2
        assert data["unique_visitors"] >= 1
        assert len(data["daily"]) == 30
        assert data["top_referrers"]

    def test_qr_png(self, client):
        create(client, code="qr1")
        r = client.get("/api/qr/qr1")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_summary(self, client):
        create(client, code="sm1")
        data = client.get("/api/stats/summary").json()
        assert data["total_links"] == 1
        assert data["site_name"]


class TestPages:
    def test_index(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "SimpleURL" in r.text

    def test_stats_page(self, client):
        create(client, code="sp1")
        r = client.get("/s/sp1")
        assert r.status_code == 200
        assert "访问统计" in r.text

    def test_healthz(self, client):
        assert client.get("/healthz").json()["status"] == "ok"

    def test_robots(self, client):
        assert client.get("/robots.txt").status_code == 200

    def test_gone_page_has_brand(self, client):
        r = client.get("/missing-code")
        assert r.status_code == 404
        assert "SimpleURL" in r.text
