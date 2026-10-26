"""加固回归：口令短码、点击上限、域名名单、纯数字短码、会话闲置、明细保留。"""

from __future__ import annotations

import datetime as dt

import pytest

from app.core import auth, runtime
from app.core.config import settings
from app.core.security import host_match, ip_hash, target_policy


def new_link(client, **payload):
    payload.setdefault("target", "https://example.com/page")
    r = client.post("/api/links", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def clean(monkeypatch):
    """把域名名单与会话设置复位，避免用例之间互相污染。"""
    monkeypatch.setattr(settings, "blocked_hosts", [])
    monkeypatch.setattr(settings, "allowed_hosts", [])
    monkeypatch.setattr(settings, "session_idle_timeout", 7200)
    runtime.reset()
    yield
    runtime.reset()


# ---------------- 域名名单 ----------------
class TestHostRules:
    def test_wildcard_matches_subdomain_only(self):
        assert host_match("a.evil.com", "*.evil.com")
        assert host_match("x.y.evil.com", "*.evil.com")
        assert not host_match("evil.com", "*.evil.com")

    def test_bare_rule_covers_subdomains(self):
        assert host_match("evil.com", "evil.com")
        assert host_match("sub.evil.com", "evil.com")
        assert not host_match("notevil.com", "evil.com")

    def test_case_and_trailing_dot_ignored(self):
        assert host_match("EVIL.com.", "evil.com")

    def test_block_reason(self):
        assert target_policy("https://a.evil.com/x", ["evil.com"], [], "off")
        assert target_policy("https://ok.com/x", ["evil.com"], [], "off") is None

    def test_allow_mode_requires_match(self):
        assert target_policy("https://ok.com/x", [], ["ok.com"], "allow") is None
        assert target_policy("https://bad.com/x", [], ["ok.com"], "allow")

    def test_create_rejects_blocked_host(self, client, clean):
        runtime.set_many({"blocked_hosts": "evil.com"})
        r = client.post("/api/links", json={"target": "https://a.evil.com/x"})
        assert r.status_code == 422
        assert "屏蔽" in r.json()["message"]

    def test_edit_cannot_switch_to_blocked_host(self, client, clean):
        """只拦创建不拦编辑，等于名单形同虚设。"""
        new_link(client, code="pol1", target="https://good.com/x")
        runtime.set_many({"blocked_hosts": "evil.com"})
        r = client.patch("/api/links/pol1", json={"target": "https://evil.com/x"})
        assert r.status_code == 422

    def test_redirect_dies_after_host_blocked(self, client, clean):
        new_link(client, code="pol2", target="https://good.com/x")
        runtime.set_many({"blocked_hosts": "good.com"})
        r = client.get("/pol2", follow_redirects=False)
        assert r.status_code == 410
        assert "不可用" in r.text

    def test_allow_list_limits_creation(self, client, clean):
        runtime.set_many({"allowed_hosts": "allowed.com"})
        assert client.post("/api/links", json={"target": "https://other.com/x"}).status_code == 422
        assert client.post("/api/links", json={"target": "https://allowed.com/x"}).status_code == 201


# ---------------- 纯数字短码 ----------------
class TestNumericCode:
    def test_numeric_rejected(self, client, clean):
        r = client.post("/api/links", json={"target": "https://e.com/x", "code": "8080"})
        assert r.status_code == 422
        assert "端口" in r.json()["message"]

    def test_alphanumeric_ok(self, client, clean):
        assert new_link(client, code="a8080")["code"] == "a8080"


# ---------------- 访问口令 ----------------
class TestPasscode:
    def test_locked_link_shows_gate(self, client, clean):
        new_link(client, code="secret", password="s3cret")
        r = client.get("/secret", follow_redirects=False)
        assert r.status_code == 200
        assert "需要口令" in r.text
        assert "example.com" not in r.text, "验证页不能泄露目标地址"

    def test_wrong_password_rejected(self, client, clean):
        new_link(client, code="secret2", password="s3cret")
        r = client.post("/secret2/unlock", data={"password": "nope"})
        assert r.status_code == 403
        assert "口令不正确" in r.text

    def test_correct_password_redirects(self, client, clean):
        new_link(client, code="secret3", password="s3cret")
        r = client.post("/secret3/unlock", data={"password": "s3cret"}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/secret3?_su_ok=1"
        # 放行标记只在当次有效：拿掉标记仍然回到验证页
        assert client.get("/secret3", follow_redirects=False).status_code == 200
        hop = client.get("/secret3?_su_ok=1", follow_redirects=False)
        assert hop.status_code == 307
        assert hop.headers["location"] == "https://example.com/page"

    def test_password_never_stored_plain(self, client, clean, db):
        from sqlalchemy import select

        from app.models import Link

        new_link(client, code="secret4", password="plain-text-pw")
        link = db.execute(select(Link).where(Link.code == "secret4")).scalar_one()
        assert link.pass_hash and "plain-text-pw" not in link.pass_hash
        assert link.to_dict()["locked"] is True

    def test_password_can_be_cleared(self, client, clean):
        new_link(client, code="secret5", password="s3cret")
        r = client.patch("/api/links/secret5", json={"clear_password": True})
        assert r.status_code == 200
        assert r.json()["locked"] is False
        assert client.get("/secret5", follow_redirects=False).status_code == 307

    def test_unlock_is_rate_limited(self, client, clean, monkeypatch):
        new_link(client, code="secret6", password="s3cret")
        auth._login_hits.clear()
        monkeypatch.setattr(auth, "login_allowed", lambda *a, **k: False)
        r = client.post("/secret6/unlock", data={"password": "s3cret"})
        assert r.status_code == 429


# ---------------- 点击上限 ----------------
class TestMaxClicks:
    def test_exhausted_link_stops(self, client, clean):
        new_link(client, code="cap1", max_clicks=2)
        for _ in range(2):
            assert client.get("/cap1", follow_redirects=False).status_code == 307
        # 第 3 次之前先结算，点击是延迟落库的
        from app.core.clickbuffer import click_buffer

        click_buffer.flush()
        r = client.get("/cap1", follow_redirects=False)
        assert r.status_code == 410
        assert "次数" in r.text

    def test_clear_cap_restores(self, client, clean):
        new_link(client, code="cap2", max_clicks=1)
        client.get("/cap2", follow_redirects=False)
        from app.core.clickbuffer import click_buffer

        click_buffer.flush()
        assert client.get("/cap2", follow_redirects=False).status_code == 410
        client.patch("/api/links/cap2", json={"clear_max_clicks": True})
        assert client.get("/cap2", follow_redirects=False).status_code == 307

    def test_state_machine(self, client, clean):
        link = new_link(client, code="cap3")
        assert link["state"] == "active"
        client.patch("/api/links/cap3", json={"enabled": False})
        assert client.get("/api/links/cap3").json()["state"] == "disabled"


# ---------------- 会话 ----------------
class TestSession:
    def test_idle_timeout_kills_session(self, client, clean, monkeypatch):
        monkeypatch.setattr(settings, "session_idle_timeout", 60)
        # 签一张两小时前的令牌
        import time

        old = auth._token(f"admin::{auth._admin_epoch()}", auth.COOKIE_MAX_AGE,
                          issued_at=int(time.time()) - 7200)
        client.cookies.set(auth.ADMIN_COOKIE, old)
        assert client.get("/api/links").status_code == 401

    def test_login_failures_lock_out(self, client, clean):
        auth._login_hits.clear()
        for _ in range(20):
            client.post("/api/admin/login", headers={"X-Admin-Token": "wrong"})
        r = client.post("/api/admin/login", headers={"X-Admin-Token": settings.admin_token})
        print("HITS", {k: len(v) for k, v in auth._login_hits.items()})
        assert r.status_code == 429

    def test_pages_are_not_cacheable(self, client, clean):
        assert "no-store" in client.get("/").headers["cache-control"]

    def test_api_is_not_cacheable(self, client, clean):
        assert "no-store" in client.get("/api/access").headers["cache-control"]

    def test_qr_keeps_its_own_cache(self, client, clean):
        new_link(client, code="qrpng")
        assert "max-age=3600" in client.get("/api/qr/qrpng").headers["cache-control"]


# ---------------- 隐私 ----------------
class TestIpHash:
    def test_hash_is_keyed(self, monkeypatch, clean):
        monkeypatch.setattr(settings, "secret_key", "key-a")
        first = ip_hash("1.2.3.4")
        monkeypatch.setattr(settings, "secret_key", "key-b")
        assert first != ip_hash("1.2.3.4")
        assert len(first) == 32

    def test_empty(self, clean):
        assert ip_hash(None) == ""


# ---------------- 明细保留 ----------------
class TestRetention:
    def test_purge_old_events(self, client, clean, db):
        from sqlalchemy import select

        from app.models import ClickEvent, Link
        from app.services import purge_old_clicks

        link = new_link(client, code="keep1")
        row = db.execute(select(Link).where(Link.code == "keep1")).scalar_one()
        old_ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400)
        db.add(ClickEvent(link_id=row.id, ts=old_ts))
        db.add(ClickEvent(link_id=row.id, ts=dt.datetime.now(dt.timezone.utc)))
        db.commit()

        removed = purge_old_clicks(days=180)
        assert removed == 1
        left = db.execute(select(ClickEvent).where(ClickEvent.link_id == row.id)).scalars().all()
        assert len(left) == 1
        db.refresh(row)
        assert row.clicks == link["clicks"], "清理明细不该动累计点击数"

    def test_zero_means_keep_forever(self, clean, monkeypatch):
        from app.services import purge_old_clicks

        assert purge_old_clicks(days=0) == 0


# ---------------- 统计 ----------------
class TestStatsExtras:
    def test_recent_list_and_device(self, client, clean):
        new_link(client, code="st1")
        client.get("/st1", headers={"user-agent": "Mozilla/5.0 (iPhone; Mobile)"})
        from app.core.clickbuffer import click_buffer

        click_buffer.flush()
        data = client.get("/api/links/st1/stats").json()
        assert data["clicks"] >= 1
        page = client.get("/s/st1").text
        assert "最近访问" in page
        assert "移动端" in page


class TestTemplateSanity:
    def test_stats_page_script_blocks_well_formed(self, client, clean):
        """脚本块被多包一层 <script> 会让整段变成语法错误，图表直接不出现。"""
        new_link(client, code="tpl1")
        page = client.get("/s/tpl1").text
        assert page.count("<script") == page.count("</script>")

    def test_stats_daily_is_sorted(self, client, clean):
        new_link(client, code="tpl2")
        client.get("/tpl2", follow_redirects=False)
        from app.core.clickbuffer import click_buffer

        click_buffer.flush()
        data = client.get("/api/links/tpl2/stats").json()
        dates = [d["date"] for d in data["daily"]]
        assert dates == sorted(dates)
        assert len(dates) == 30
