"""v4：回执链接、访问边界、短码长度可调、时区显示。"""

from __future__ import annotations

import re

import pytest

from app.core import claim
from app.core.db import session_scope
from app.core.settings_store import set_value
from app.core.timeutil import fmt_local
from app.services import get_link_by_code


def make_link(client, code="rcpt1", target="https://example.com/page"):
    r = client.post("/api/links", json={"code": code, "target": target})
    assert r.status_code == 201, r.text
    return r.json()


class TestReceipt:
    """开放模式下，访客建完凭回执能看自己那条，别人那条看不了。"""

    def test_guest_creation_returns_receipt(self, anon_client):
        data = make_link(anon_client)
        assert data["claim_token"], "访客创建必须拿到领取码"
        assert "t=" in data["stats_url"]

    def test_admin_creation_has_no_receipt(self, client):
        data = make_link(client)
        assert data["claim_token"] is None
        assert data["stats_url"].endswith("/s/rcpt1")

    def test_receipt_opens_only_its_own_link(self, anon_client, client):
        mine = make_link(anon_client, code="mine1")
        make_link(client, code="other1")
        token = mine["claim_token"]
        assert anon_client.get(f"/s/mine1?t={token}").status_code == 200
        assert anon_client.get(f"/s/other1?t={token}").status_code == 401

    def test_receipt_stats_api_and_qr(self, anon_client):
        data = make_link(anon_client)
        token = data["claim_token"]
        assert anon_client.get(f"/api/links/rcpt1/stats?t={token}").status_code == 200
        assert anon_client.get(f"/api/qr/rcpt1?t={token}").status_code == 200
        assert anon_client.get("/api/qr/rcpt1").status_code == 401

    def test_wrong_receipt_rejected(self, anon_client):
        make_link(anon_client)
        assert anon_client.get("/s/rcpt1?t=not-a-real-token").status_code == 401

    def test_receipt_page_embeds_working_qr(self, anon_client):
        data = make_link(anon_client)
        html = anon_client.get(f"/s/rcpt1?t={data['claim_token']}").text
        src = re.search(r'<img src="([^"]*api/qr[^"]*)"', html).group(1)
        assert "t=" in src
        assert anon_client.get(src.replace("http://testserver", "")).status_code == 200

    def test_claim_hash_never_leaks(self, anon_client, client):
        data = make_link(anon_client)
        dumped = client.get("/api/links/rcpt1").text
        assert data["claim_token"] not in dumped
        with session_scope() as db:
            link = get_link_by_code(db, "rcpt1")
            assert link.claim_hash and link.claim_hash != data["claim_token"]


class TestGuestBoundary:
    """开放模式只开放「建」，不开放「看别人建的」。"""

    def test_guest_cannot_browse_admin_surfaces(self, anon_client):
        for path in ("/api/links", "/api/links/export", "/api/stats/summary"):
            r = anon_client.get(path)
            if path == "/api/stats/summary":
                assert r.status_code == 200
                assert r.json()["total_links"] is None
            else:
                assert r.status_code == 401, path

    def test_guest_page_hides_board_hint(self, anon_client):
        html = anon_client.get("/").text
        assert "链接库" in html  # 区块还在，但前端按身份切成说明板


class TestCodeLength:
    def test_default_code_is_four_chars(self, client):
        r = client.post("/api/links", json={"target": "https://example.com/z"})
        assert r.status_code == 201
        assert len(r.json()["code"]) == 4
        assert re.fullmatch(r"[A-Za-z0-9]{4}", r.json()["code"])

    def test_length_is_configurable_from_admin(self, client):
        r = client.post("/api/admin/settings", json={"code_length": 8})
        assert r.status_code == 200
        assert client.get("/api/admin/settings").json()["code_length"] == 8
        r = client.post("/api/links", json={"target": "https://example.com/q"})
        assert len(r.json()["code"]) == 8

    def test_length_clamped(self, client):
        client.post("/api/admin/settings", json={"code_length": 99})
        assert client.get("/api/admin/settings").json()["code_length"] == 16
        client.post("/api/admin/settings", json={"code_length": 1})
        assert client.get("/api/admin/settings").json()["code_length"] == 3

    def test_stored_value_used_by_service(self, client):
        with session_scope() as db:
            set_value(db, "code_length", 6)
        r = client.post("/api/links", json={"target": "https://example.com/w"})
        assert len(r.json()["code"]) == 6


class TestTimezone:
    def test_fmt_local_renders_local_time(self):
        import datetime as dt

        value = dt.datetime(2026, 1, 2, 3, 4, tzinfo=dt.timezone.utc)
        assert fmt_local(value) == value.astimezone().strftime("%Y-%m-%d %H:%M")

    def test_stats_page_shows_local_not_utc(self, client):
        data = make_link(client, code="tz1")
        html = client.get("/s/tz1").text
        assert "created_at" not in html
        created = data["created_at"]
        assert created  # 页面把时间交给 fmt_local，不再直接 strftime


@pytest.mark.parametrize("path", ["/s/rcpt1", "/api/links/rcpt1/stats"])
def test_closed_receipt_after_revoke(anon_client, client, path):
    data = make_link(anon_client, code="rcpt1")
    token = data["claim_token"]
    url = f"{path}?t={token}"
    assert anon_client.get(url).status_code == 200
    with session_scope() as db:
        claim.revoke(db, get_link_by_code(db, "rcpt1"))
    assert anon_client.get(url).status_code == 401
