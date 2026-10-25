"""查重与短码预检：创建前的顺手校验。"""

from __future__ import annotations


def make(client, **payload):
    payload.setdefault("target", "https://example.com/page")
    r = client.post("/api/links", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


class TestCodeAvailable:
    def test_free_code(self, client):
        r = client.get("/api/codes/brand-new/available")
        assert r.status_code == 200
        assert r.json() == {"code": "brand-new", "available": True, "message": "可以用"}

    def test_taken_code(self, client):
        make(client, code="taken1")
        data = client.get("/api/codes/taken1/available").json()
        assert data["available"] is False
        assert "占用" in data["message"]

    def test_case_insensitive(self, client):
        make(client, code="Mixed1")
        assert client.get("/api/codes/mixed1/available").json()["available"] is False

    def test_too_short(self, client):
        data = client.get("/api/codes/ab/available").json()
        assert data["available"] is False

    def test_illegal_chars(self, client):
        data = client.get("/api/codes/a.b/available").json()
        assert data["available"] is False

    def test_guest_sees_no_occupancy(self, guest):
        # 访客不暴露占用情况，免得短码库被人拿这个接口扫出来。
        # 注意访客看谁都说「可以用」，所以这里只能断言不泄露，不能断言真实结果。
        from app.core.config import settings

        settings.access_mode = "admin"
        try:
            data = guest.get("/api/codes/anything/available").json()
        finally:
            settings.access_mode = "public"
        assert data["available"] is True and data["message"] == ""


class TestDuplicate:
    def test_finds_same_target(self, client):
        make(client, code="dup1", target="https://example.com/dup-page")
        items = client.get("/api/admin/duplicate", params={"target": "https://example.com/dup-page"}).json()["items"]
        assert [i["code"] for i in items] == ["dup1"]

    def test_ignores_trailing_slash_and_fragment(self, client):
        make(client, code="dup2", target="https://example.com/clean")
        for probe in ("https://example.com/clean/", "https://example.com/clean#top", "http://example.com/clean"):
            items = client.get("/api/admin/duplicate", params={"target": probe}).json()["items"]
            assert any(i["code"] == "dup2" for i in items), probe

    def test_different_path_not_duplicate(self, client):
        make(client, code="dup3", target="https://example.com/a/b")
        items = client.get("/api/admin/duplicate", params={"target": "https://example.com/a/bc"}).json()["items"]
        assert items == []

    def test_sorted_by_clicks(self, client):
        make(client, code="low1", target="https://example.com/hot")
        make(client, code="high1", target="https://example.com/hot")
        client.get("/high1")
        from app.core.clickbuffer import click_buffer

        click_buffer.flush()
        items = client.get("/api/admin/duplicate", params={"target": "https://example.com/hot"}).json()["items"]
        assert [i["code"] for i in items] == ["high1", "low1"]

    def test_guest_forbidden(self, guest):
        assert guest.get("/api/admin/duplicate", params={"target": "https://example.com/x"}).status_code in (401, 403)
