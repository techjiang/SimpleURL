"""v10 专项：创建前把话说清楚。

两条被钉住的教训：
1. 校验要在提交之前。填完一整屏才被告知「已被占用」，是拿用户的时间换实现省事。
2. 「已经在库里了」不算错误，是一次复用机会——但别把别人的短码递出去。
   所以查重只给管理员，短码预检对没有创建权的人一律说「可以用」。
"""

from __future__ import annotations


def make_link(client, **payload):
    payload.setdefault("target", "https://example.com/page")
    payload.setdefault("fetch_title", False)
    r = client.post("/api/links", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


class TestCodeAvailable:
    def test_free_code(self, client):
        r = client.get("/api/codes/brand-new/available")
        assert r.status_code == 200
        assert r.json() == {"code": "brand-new", "available": True, "message": "可以用"}

    def test_taken_code(self, client):
        make_link(client, code="taken1")
        data = client.get("/api/codes/taken1/available").json()
        assert data["available"] is False
        assert "占用" in data["message"]

    def test_case_insensitive(self, client):
        # 域名不区分大小写，`Mixed1` 和 `mixed1` 落同一个地址，必须拦
        make_link(client, code="Mixed1")
        assert client.get("/api/codes/mixed1/available").json()["available"] is False

    def test_too_short(self, client):
        assert client.get("/api/codes/ab/available").json()["available"] is False

    def test_illegal_chars(self, client):
        data = client.get("/api/codes/a.b/available").json()
        assert data["available"] is False
        assert "仅支持" in data["message"]

    def test_reserved_word(self, client):
        # admin 是保留字，创建时会被拒，预检不能反过来哄人说能用
        assert client.get("/api/codes/admin/available").json()["available"] is False

    def test_guest_learns_nothing(self, anon_client):
        # 访客没有创建权，接口一律回「可以用」，否则这里就是个短码库扫描器。
        # 开放模式下访客本来就能建，所以这里把站点设成 admin 模式再问。
        from app.core.db import session_scope
        from app.core.settings_store import set_value

        with session_scope() as db:
            set_value(db, "access_mode", "admin")
        data = anon_client.get("/api/codes/anything/available").json()
        assert data == {"code": "anything", "available": True, "message": ""}


class TestDuplicate:
    def test_finds_same_target(self, client):
        make_link(client, code="dup1", target="https://example.com/dup-page")
        items = client.get("/api/duplicate", params={"target": "https://example.com/dup-page"}).json()["items"]
        assert [i["code"] for i in items] == ["dup1"]

    def test_ignores_slash_fragment_and_scheme(self, client):
        make_link(client, code="dup2", target="https://example.com/clean")
        for probe in ("https://example.com/clean/", "https://example.com/clean#top", "http://example.com/clean"):
            items = client.get("/api/duplicate", params={"target": probe}).json()["items"]
            assert any(i["code"] == "dup2" for i in items), probe

    def test_different_path_not_duplicate(self, client):
        # 前缀相同不算重复：/a/b 和 /a/bc 是两个页面
        make_link(client, code="dup3", target="https://example.com/a/b")
        items = client.get("/api/duplicate", params={"target": "https://example.com/a/bc"}).json()["items"]
        assert items == []

    def test_query_string_matters(self, client):
        make_link(client, code="dup4", target="https://example.com/q?x=1")
        items = client.get("/api/duplicate", params={"target": "https://example.com/q?x=2"}).json()["items"]
        assert items == []

    def test_sorted_by_clicks(self, client, flush_clicks):
        make_link(client, code="lowone", target="https://example.com/hot")
        make_link(client, code="highone", target="https://example.com/hot")
        client.get("/highone")
        flush_clicks()
        items = client.get("/api/duplicate", params={"target": "https://example.com/hot"}).json()["items"]
        assert [i["code"] for i in items] == ["highone", "lowone"]

    def test_guest_forbidden(self, anon_client):
        # 结果里有别人建的短码，开放给访客等于给链接库开后门
        assert anon_client.get("/api/duplicate", params={"target": "https://example.com/x"}).status_code in (401, 403)
