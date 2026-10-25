"""v3 能力测试：延迟计数、防洪、UA 屏蔽、标题、迁移、后台、YOURLS API。"""

from __future__ import annotations

import time

import pytest

from app.core import guard
from app.core.clicks import click_buffer
from app.core.titles import guess_title
from app.core.yourls import parse
from app.models import ApiKey, Link, Setting


class TestDelayedClicks:
    def test_buffer_then_flush(self, client, flush_clicks):
        client.post("/api/links", json={"target": "https://example.com/a", "code": "dc1"})
        client.get("/dc1")
        client.get("/dc1")
        client.get("/dc1")
        assert click_buffer.pending() == 3
        assert flush_clicks() == 3
        assert click_buffer.pending() == 0
        assert client.get("/api/links/dc1").json()["clicks"] == 3

    def test_flush_writes_events(self, client, flush_clicks):
        client.post("/api/links", json={"target": "https://example.com/a", "code": "dc2"})
        client.get("/dc2")
        flush_clicks()
        data = client.get("/api/links/dc2/stats").json()
        assert data["clicks"] == 1
        assert data["unique_visitors"] == 1

    def test_flush_failure_requeues(self, db):
        """写库失败时缓冲要原样退回，不能丢数据。"""
        from app.core.db import session_scope
        from app.core.timeutil import utcnow
        from app.services import create_link
        from app.schemas import LinkCreate

        with session_scope() as s:
            link = create_link(s, LinkCreate(target="https://example.com/a", code="dc3"))
            link_id = link.id
        click_buffer.add(db, link_id, None, "curl/8.4.0", "1.1.1.1")
        assert click_buffer.pending() == 1
        # 掺一条指向不存在链接的记录，外键会整批拦下
        pending_cls = type(click_buffer._queue[0])
        click_buffer._queue.append(pending_cls(9999, None, "curl", "bot", None, "1.1.1.1", False, utcnow()))
        with pytest.raises(Exception):
            click_buffer.flush()
        assert click_buffer.pending() == 2
        # 清掉坏记录后能正常落库
        click_buffer._queue.pop()
        assert click_buffer.flush() == 1
        assert click_buffer.pending() == 0


class TestUaBlocking:
    def test_substring_rule(self, db):
        rules = guard.parse_list("curl/\npython-requests")
        assert guard.is_ua_blocked("curl/8.4.0", rules) == "curl/"
        assert guard.is_ua_blocked("Mozilla/5.0 Chrome/131", rules) is None

    def test_wildcard_rule(self):
        rules = ["BadBot*", "GPTBot"]
        assert guard.is_ua_blocked("BadBot/2.1", rules) == "BadBot*"
        assert guard.is_ua_blocked("GPTBot/1.0", rules) == "GPTBot"

    def test_blocked_click_not_counted(self, client, flush_clicks, db):
        from app.core.db import session_scope

        client.post("/api/links", json={"target": "https://example.com/a", "code": "ub1"})
        with session_scope() as s:
            guard.save_ua_rules(s, ["curl/"])
        r = client.get("/ub1", headers={"user-agent": "curl/8.4.0"}, follow_redirects=False)
        assert r.status_code == 307  # 仍然跳转，只是不计数
        flush_clicks()
        link = client.get("/api/links/ub1").json()
        assert link["clicks"] == 0

    def test_normal_ua_still_counted(self, client, flush_clicks):
        client.post("/api/links", json={"target": "https://example.com/a", "code": "ub2"})
        client.get("/ub2", headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/131"})
        assert flush_clicks() == 1
        assert client.get("/api/links/ub2").json()["clicks"] == 1


class TestRedirectRateLimit:
    def test_rate_limit_triggers(self, client, monkeypatch):
        from app.core import guard as guard_mod

        client.post("/api/links", json={"target": "https://example.com/a", "code": "rl1"})
        # 把上限压到 3，连打 4 次
        monkeypatch.setattr(guard_mod.rate_limiter, "allow", lambda key, limit, window: False)
        r = client.get("/rl1", follow_redirects=False)
        assert r.status_code == 429
        assert "频繁" in r.text

    def test_quota_allows_normal(self, client):
        client.post("/api/links", json={"target": "https://example.com/a", "code": "rl2"})
        assert client.get("/rl2", follow_redirects=False).status_code == 307


class TestTitleGuess:
    def test_slug(self):
        assert guess_title("https://example.com/blog/2026/hello-world.html") == "hello world"

    def test_root(self):
        assert guess_title("https://example.com") == "example.com"

    def test_strip_extension(self):
        assert guess_title("https://x.com/a/b/my-page.php") == "my page"


class TestYourlsParse:
    def test_parse_json(self):
        rows = parse('{"abc": "https://example.com/a"}')
        assert rows == [{"code": "abc", "target": "https://example.com/a"}]

    def test_parse_json_list(self):
        rows = parse('[{"keyword":"k1","url":"https://a.com","clicks":"5"}]')
        assert rows[0]["code"] == "k1" and rows[0]["clicks"] == 5

    def test_parse_sql(self):
        sql = (
            "INSERT INTO `yourls_url` (`keyword`, `url`, `title`, `timestamp`, `clicks`) VALUES "
            "('k1', 'https://example.com/a', '标题', '1700000000', '9');"
        )
        rows = parse(sql)
        assert rows[0]["code"] == "k1"
        assert rows[0]["clicks"] == 9
        assert rows[0]["created_at"].startswith("2023-11-14")

    def test_parse_sql_with_commas_in_url(self):
        sql = (
            "INSERT INTO `yourls_url` (`keyword`, `url`) VALUES "
            "('k1', 'https://example.com/a,b,c');"
        )
        assert parse(sql)[0]["target"] == "https://example.com/a,b,c"

    def test_garbage_rejected(self):
        import pytest

        with pytest.raises(ValueError):
            parse("这不是你的URLS也不是SQL")


class TestMigration:
    def _login(self, client):
        r = client.post("/api/admin/login", json={"username": "admin", "password": "test-token-please-rotate"})
        assert r.status_code == 200, r.text

    def test_migrate_json(self, client):
        self._login(client)
        r = client.post(
            "/api/admin/migrate/yourls",
            json={"payload": '{"mg1": "https://example.com/a", "mg2": "https://example.com/b"}'},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["imported"] == 2 and body["total"] == 2
        # 短码长度合法（>=3）才会原样保留
        assert client.get("/mg1", follow_redirects=False).status_code == 307
        assert client.get("/mg2", follow_redirects=False).status_code == 307

    def test_migrate_conflict_renames(self, client):
        self._login(client)
        client.post("/api/links", json={"target": "https://example.com/orig", "code": "dup1"})
        r = client.post(
            "/api/admin/migrate/yourls", json={"payload": '{"dup1": "https://example.com/new"}'}
        )
        body = r.json()
        assert body["renamed"] == 1
        # 原链接没被顶掉
        assert client.get("/api/links/dup1").json()["target"] == "https://example.com/orig"

    def test_migrate_bad_payload(self, client):
        self._login(client)
        r = client.post("/api/admin/migrate/yourls", json={"payload": "乱码内容"})
        assert r.status_code == 422

    def test_migrate_requires_login(self, client, anon_client):
        r = anon_client.post("/api/admin/migrate/yourls", json={"payload": "{}"})
        assert r.status_code == 401


class TestAdminAuth:
    def test_login_logout(self, client):
        r = client.post("/api/admin/login", json={"username": "admin", "password": "test-token-please-rotate"})
        assert r.status_code == 200
        assert client.get("/api/admin/session").json()["admin"] == "admin"
        client.post("/api/admin/logout")
        assert client.get("/api/admin/session").json()["admin"] is None

    def test_wrong_password(self, client):
        r = client.post("/api/admin/login", json={"username": "admin", "password": "nope"})
        assert r.status_code == 401

    def test_token_tamper_rejected(self, client):
        r = client.post("/api/admin/login", json={"username": "admin", "password": "test-token-please-rotate"})
        assert r.status_code == 200
        token = client.cookies.get("su_admin")
        assert token
        # 直接改 header 里的 cookie，绕开 TestClient 的 jar
        r = client.get("/api/admin/session", headers={"Cookie": f"su_admin={token[:-4]}0000"})
        assert r.json()["admin"] is None
        r = client.get("/api/admin/session", headers={"Cookie": f"su_admin={token}"})
        assert r.json()["admin"] == "admin"


class TestAdminDashboard:
    def test_dashboard(self, client, flush_clicks):
        client.post("/api/admin/login", json={"username": "admin", "password": "test-token-please-rotate"})
        client.post("/api/links", json={"target": "https://example.com/a", "code": "db1"})
        client.post("/api/links", json={"target": "https://example.com/b", "code": "db2"})
        client.get("/db1")
        flush_clicks()
        r = client.get("/api/admin/dashboard")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total_links"] == 2
        assert body["total_clicks"] == 1
        assert body["enabled_links"] == 2
        assert len(body["daily"]) == 14
        assert body["top_links"][0]["code"] == "db1"

    def test_dashboard_requires_login(self, client, anon_client):
        assert anon_client.get("/api/admin/dashboard").status_code == 401


class TestAdminSettings:
    def test_save_and_read(self, client, db):
        client.post("/api/admin/login", json={"username": "admin", "password": "test-token-please-rotate"})
        r = client.post(
            "/api/admin/settings",
            json={"redirect_rate_limit": 30, "fetch_title": False, "site_name": "我的短链"},
        )
        assert r.status_code == 200, r.text
        body = client.get("/api/admin/settings").json()
        assert body["redirect_rate_limit"] == 30
        assert body["fetch_title"] is False
        assert body["site_name"] == "我的短链"

    def test_reset(self, client):
        client.post("/api/admin/login", json={"username": "admin", "password": "test-token-please-rotate"})
        client.post("/api/admin/settings", json={"redirect_rate_limit": 5})
        client.post("/api/admin/settings/reset")
        assert client.get("/api/admin/settings").json()["redirect_rate_limit"] == 120

    def test_ua_rules_roundtrip(self, client):
        client.post("/api/admin/login", json={"username": "admin", "password": "test-token-please-rotate"})
        r = client.post("/api/admin/ua-rules", json={"rules": "curl/\nBadBot\n"})
        assert r.json()["rules"] == ["curl/", "BadBot"]
        assert client.get("/api/admin/ua-rules").json()["rules"] == ["curl/", "BadBot"]

    def test_flush_endpoint(self, client):
        client.post("/api/admin/login", json={"username": "admin", "password": "test-token-please-rotate"})
        client.post("/api/links", json={"target": "https://example.com/a", "code": "fc1"})
        client.get("/fc1")
        r = client.post("/api/admin/flush-clicks")
        assert r.status_code == 200
        assert r.json()["written"] == 1


class TestApiKeys:
    def test_key_lifecycle(self, client, db):
        client.post("/api/admin/login", json={"username": "admin", "password": "test-token-please-rotate"})
        r = client.post("/api/admin/api-keys", json={"name": "测试"})
        assert r.status_code == 201, r.text
        key = r.json()["key"]
        assert key.startswith("sk-")
        assert len(client.get("/api/admin/api-keys").json()) == 1
        key_id = r.json()["id"]
        assert client.delete(f"/api/admin/api-keys/{key_id}").status_code == 200
        assert client.get("/api/admin/api-keys").json() == []

    def test_require_key_blocks_then_allows(self, client, db, monkeypatch):
        client.post("/api/admin/login", json={"username": "admin", "password": "test-token-please-rotate"})
        client.post("/api/admin/settings", json={"require_api_key": True})
        assert client.get("/api/links").status_code == 401
        key = client.post("/api/admin/api-keys", json={}).json()["key"]
        assert client.get("/api/links", headers={"X-Api-Key": key}).status_code == 200
        assert client.get("/api/links?key=" + key).status_code == 200


class TestYourlsApi:
    def test_shorturl(self, client):
        r = client.post("/yourls-api?action=shorturl&url=https://example.com/yl")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "success"
        assert body["url"]["url"] == "https://example.com/yl"

    def test_shorturl_custom_keyword(self, client):
        r = client.post("/yourls-api?action=shorturl&url=https://example.com/yl&keyword=yl1")
        assert r.json()["url"]["keyword"] == "yl1"

    def test_expand(self, client):
        client.post("/yourls-api?action=shorturl&url=https://example.com/x&keyword=ex1")
        r = client.post("/yourls-api?action=expand&shorturl=http://t/ex1")
        assert r.json()["longurl"] == "https://example.com/x"

    def test_url_stats(self, client, flush_clicks):
        client.post("/yourls-api?action=shorturl&url=https://example.com/x&keyword=us1")
        client.get("/us1")
        flush_clicks()
        r = client.post("/yourls-api?action=url-stats&shorturl=http://t/us1")
        assert r.json()["stats"]["total"] == 1

    def test_db_stats(self, client):
        r = client.post("/yourls-api?action=db-stats")
        assert r.status_code == 200
        assert "total_links" in r.json()["stats"]

    def test_missing_url_fails(self, client):
        r = client.post("/yourls-api?action=shorturl")
        assert r.json()["status"] == "fail"

    def test_signature_required(self, client, monkeypatch):
        monkeypatch.setattr("app.yourls_api.settings.yourls_token", "secret")
        assert client.post("/yourls-api?action=db-stats").status_code == 403
        assert client.post("/yourls-api?action=db-stats&signature=secret").status_code == 200


class TestSettingsStore:
    def test_effective_default_and_override(self, db):
        from app.core.settings_store import effective, set_value

        assert effective(db, "redirect_rate_limit") == 120
        set_value(db, "redirect_rate_limit", 42)
        assert effective(db, "redirect_rate_limit") == 42

    def test_bool_coercion(self, db):
        from app.core.settings_store import effective, set_value

        set_value(db, "fetch_title", False)
        assert effective(db, "fetch_title") is False
        set_value(db, "fetch_title", True)
        assert effective(db, "fetch_title") is True

    def test_unknown_key_rejected(self, db):
        import pytest

        from app.core.settings_store import set_value

        with pytest.raises(KeyError):
            set_value(db, "not_a_real_key", 1)


class TestAutoTitle:
    def test_fallback_slug_used_when_unreachable(self, client):
        """目标站连不上时用 URL 兜底，不让 title 空着。"""
        r = client.post("/api/links", json={"target": "https://nonexistent.invalid/some-page"})
        assert r.status_code == 201
        assert r.json()["title"] == "some page"

    def test_explicit_title_wins(self, client):
        r = client.post(
            "/api/links",
            json={"target": "https://example.com/a/b", "title": "我指定的标题"},
        )
        assert r.json()["title"] == "我指定的标题"

    def test_fetch_disabled_keeps_empty(self, client):
        client.post("/api/admin/login", json={"username": "admin", "password": "test-token-please-rotate"})
        client.post("/api/admin/settings", json={"fetch_title": False})
        r = client.post("/api/links", json={"target": "https://example.com/x/y/z"})
        assert r.json()["title"] is None

    def test_ssrf_guard_blocks_private(self):
        import asyncio

        from app.core.titles import fetch_title, _is_safe_host

        assert _is_safe_host("http://127.0.0.1:8080/admin") is False
        assert _is_safe_host("http://169.254.169.254/latest/meta-data") is False
        assert _is_safe_host("https://example.com") is True
        assert asyncio.run(fetch_title("http://127.0.0.1:1/")) is None


class TestSummaryExcludesBlocked:
    def test_total_clicks_matches_links(self, client, flush_clicks):
        client.post("/api/links", json={"target": "https://example.com/a", "code": "sm1"})
        client.post("/api/admin/login", json={"username": "admin", "password": "test-token-please-rotate"})
        client.post("/api/admin/ua-rules", json={"rules": "curl/"})
        client.get("/sm1", headers={"user-agent": "Mozilla/5.0 Chrome/131"})
        client.get("/sm1", headers={"user-agent": "curl/8.4.0"})
        flush_clicks()
        summary = client.get("/api/stats/summary").json()
        link = client.get("/api/links/sm1").json()
        # 首页总点击要和链接点击数一致，不能把被拦截的算进去
        assert summary["total_clicks"] == link["clicks"] == 1

    def test_blocked_events_still_recorded(self, client, flush_clicks):
        client.post("/api/links", json={"target": "https://example.com/a", "code": "sm2"})
        client.post("/api/admin/login", json={"username": "admin", "password": "test-token-please-rotate"})
        client.post("/api/admin/ua-rules", json={"rules": "curl/"})
        client.get("/sm2", headers={"user-agent": "curl/8.4.0"})
        flush_clicks()
        dash = client.get("/api/admin/dashboard").json()
        assert dash["blocked_events"] == 1
        assert dash["total_clicks"] == 0
