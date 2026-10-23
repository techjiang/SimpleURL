"""新增能力测试：编辑、导出、设备统计、UA 解析、访问令牌。"""

from __future__ import annotations

import json

from app.core.security import parse_user_agent
from app.services import links_to_csv


class TestUserAgent:
    def test_desktop(self):
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
        name, device = parse_user_agent(ua)
        assert name == "Chrome" and device == "desktop"

    def test_mobile(self):
        ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Version/17.0 Mobile/15E148 Safari/604.1"
        name, device = parse_user_agent(ua)
        assert device == "mobile"

    def test_bot(self):
        name, device = parse_user_agent("curl/8.4.0")
        assert name == "机器人 / 脚本" and device == "bot"

    def test_empty(self):
        assert parse_user_agent("") == ("未知", "unknown")

    def test_wechat(self):
        name, _ = parse_user_agent("Mozilla/5.0 ... MicroMessenger/8.0.49")
        assert name == "微信"


class TestEdit:
    def test_edit_full(self, client):
        r = client.post("/api/links", json={"target": "https://example.com/a", "code": "ed1"})
        assert r.status_code == 201
        r = client.patch(
            "/api/links/ed1",
            json={"target": "https://example.com/b", "title": "新标题", "tags": ["x", "y"]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["target"] == "https://example.com/b"
        assert body["title"] == "新标题"
        assert body["tags"] == ["x", "y"]

    def test_edit_clear_expiry(self, client):
        client.post("/api/links", json={"target": "https://example.com/a", "code": "ed2",
                                        "expires_at": "2099-01-01T00:00:00Z"})
        r = client.patch("/api/links/ed2", json={"expires_at": None})
        assert r.status_code == 200
        assert r.json()["expires_at"] is None
        assert r.json()["expired"] is False

    def test_edit_reject_bad_target(self, client):
        client.post("/api/links", json={"target": "https://example.com/a", "code": "ed3"})
        r = client.patch("/api/links/ed3", json={"target": "javascript:alert(1)"})
        assert r.status_code == 422

    def test_edit_past_expiry_rejected(self, client):
        client.post("/api/links", json={"target": "https://example.com/a", "code": "ed4"})
        r = client.patch("/api/links/ed4", json={"expires_at": "2000-01-01T00:00:00Z"})
        assert r.status_code == 422

    def test_edit_missing(self, client):
        r = client.patch("/api/links/nope", json={"title": "x"})
        assert r.status_code == 404


class TestDeviceStats:
    def test_device_breakdown(self, client, flush_clicks):
        client.post("/api/links", json={"target": "https://example.com/a", "code": "dv1"})
        client.get("/dv1", headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/131.0 Safari/537.36"})
        client.get("/dv1", headers={"user-agent": "Mozilla/5.0 (iPhone) Version/17.0 Mobile Safari/604.1"})
        assert flush_clicks() == 2
        data = client.get("/api/links/dv1/stats").json()
        names = {d["device"]: d["count"] for d in data["devices"]}
        assert names.get("桌面端") == 1
        assert names.get("移动端") == 1

    def test_bot_recorded(self, client, flush_clicks):
        client.post("/api/links", json={"target": "https://example.com/a", "code": "dv2"})
        client.get("/dv2", headers={"user-agent": "curl/8.4.0"})
        assert flush_clicks() == 1
        data = client.get("/api/links/dv2/stats").json()
        assert any(d["device"] == "机器人 / 脚本" for d in data["devices"])


class TestExport:
    def test_csv_download(self, client):
        client.post("/api/links", json={"target": "https://example.com/a", "code": "ex1", "tags": ["标签"]})
        r = client.get("/api/links/export")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert "attachment" in r.headers["content-disposition"]
        text = r.text
        assert "短码" in text and "ex1" in text and "标签" in text

    def test_csv_respects_filter(self, client):
        client.post("/api/links", json={"target": "https://example.com/a", "code": "ex2"})
        client.post("/api/links", json={"target": "https://example.com/b", "code": "ex3"})
        r = client.get("/api/links/export?q=ex3")
        assert "ex3" in r.text
        assert "ex2," not in r.text

    def test_csv_utf8_bom(self, client):
        client.post("/api/links", json={"target": "https://example.com/a", "code": "ex4"})
        r = client.get("/api/links/export")
        assert r.content.startswith(b"\xef\xbb\xbf")


class TestAccessToken:
    def test_gate_closed_by_default(self, client):
        client.post("/api/links", json={"target": "https://example.com/a", "code": "gt1"})
        assert client.get("/gt1", follow_redirects=False).status_code == 307
        assert client.get("/s/gt1").status_code == 200

    def test_gate_open_requires_token(self, client, monkeypatch):
        from app.core.config import settings
        from app.services import ServiceError

        monkeypatch.setattr(settings, "access_token", "secret")
        try:
            client.post("/api/links", json={"target": "https://example.com/a", "code": "gt2"})
            assert client.get("/gt2", follow_redirects=False).status_code == 403
            r = client.get("/gt2?token=secret", follow_redirects=False)
            assert r.status_code == 307
            assert client.get("/s/gt2?token=secret").status_code == 200
        finally:
            monkeypatch.setattr(settings, "access_token", "")

    def test_reserved_path_not_shortened(self, client):
        assert client.get("/healthz").status_code == 200
        assert client.get("/robots.txt").status_code == 200


class TestCsvHelper:
    def test_csv_helper(self, db):
        from app.schemas import LinkCreate
        from app.services import create_link

        create_link(db, LinkCreate(target="https://example.com/a", code="cs1", tags=["t"]), ip=None)
        db.commit()
        from sqlalchemy import select
        from app.models import Link

        link = db.execute(select(Link).where(Link.code == "cs1")).scalar_one()
        text = links_to_csv([link])
        assert "cs1" in text and "t" in text
        assert "短码,短链接" in text
