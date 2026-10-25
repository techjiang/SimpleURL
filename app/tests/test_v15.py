"""v15：删除不再是一次性的，到期不再没人提醒，数据不再拿不走。

三条都是「用久了才会疼」的问题，各自能对上具体场景：

1. 手滑删掉一条发出去半年的短链 —— 以前直接 DELETE，谁都救不回来
2. 链接过期了没人知道 —— 提示只在仪表盘摆着，得有人点开才看得见
3. 想搬走整个库 —— 导出只有链接，账号 / 配置 / 点击明细还在原地
"""

from __future__ import annotations

import datetime as dt


def create(client, **payload):
    payload.setdefault("target", "https://example.com/page")
    payload.setdefault("fetch_title", False)
    r = client.post("/api/links", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


class TestTrash:
    """删除 = 进回收站，30 天内可恢复。"""

    def test_delete_moves_to_trash_not_away(self, client):
        create(client, code="oops")
        assert client.delete("/api/links/oops").status_code == 200
        # 列表里没有了
        assert client.get("/api/links").json()["total"] == 0
        # 但回收站里有
        items = client.get("/api/trash").json()["items"]
        assert [i["code"] for i in items] == ["oops"]

    def test_deleted_link_does_not_redirect(self, client):
        create(client, code="gone1", target="https://example.com/a")
        client.delete("/api/links/gone1")
        r = client.get("/gone1", follow_redirects=False)
        assert r.status_code == 404
        assert "text/html" in r.headers["content-type"]

    def test_restore_brings_it_back(self, client):
        create(client, code="back1", target="https://example.com/a")
        client.delete("/api/links/back1")
        r = client.post("/api/trash/back1/restore")
        assert r.status_code == 200, r.text
        assert r.json()["item"]["code"] == "back1"
        assert client.get("/api/links").json()["total"] == 1
        assert client.get("/back1", follow_redirects=False).status_code == 307

    def test_restore_enables_again(self, client):
        create(client, code="back2")
        client.delete("/api/links/back2")
        client.post("/api/trash/back2/restore")
        assert client.get("/api/links/back2").json()["enabled"] is True

    def test_restore_twice_is_conflict(self, client):
        create(client, code="back3")
        client.delete("/api/links/back3")
        assert client.post("/api/trash/back3/restore").status_code == 200
        # 已经在库里活着了，再恢复一次说清楚，而不是假装成功
        assert client.post("/api/trash/back3/restore").status_code == 409

    def test_code_stays_taken_while_in_trash(self, client):
        """回收站里的短码仍然占用：否则恢复时会跟新建的撞车。"""
        create(client, code="held")
        client.delete("/api/links/held")
        r = client.post("/api/links", json={"target": "https://example.com/x", "code": "held", "fetch_title": False})
        assert r.status_code == 409

    def test_purge_is_permanent(self, client):
        create(client, code="burn")
        client.delete("/api/links/burn")
        assert client.delete("/api/trash/burn").status_code == 200
        assert client.get("/api/trash").json()["items"] == []
        # 彻底删掉之后短码可以重新用
        create(client, code="burn")

    def test_empty_trash(self, client):
        create(client, code="tr1")
        create(client, code="tr2")
        client.delete("/api/links/tr1")
        client.delete("/api/links/tr2")
        r = client.post("/api/trash/empty")
        assert r.json()["removed"] == 2
        assert client.get("/api/trash").json()["items"] == []

    def test_bulk_delete_also_goes_to_trash(self, client):
        create(client, code="bl1")
        create(client, code="bl2")
        r = client.post("/api/links/bulk", json={"action": "delete", "codes": ["bl1", "bl2"]})
        assert r.json()["affected"] == 2
        assert {i["code"] for i in client.get("/api/trash").json()["items"]} == {"bl1", "bl2"}

    def test_expired_trash_is_purged(self, client, db):
        """回收站只留 30 天：不清理的话，删除只是换个地方堆着。"""
        from app.models import Link
        from app.core.timeutil import utcnow
        from app.services import TRASH_RETENTION_DAYS, purge_expired_trash

        create(client, code="stale")
        client.delete("/api/links/stale")
        row = db.query(Link).filter(Link.code == "stale").one()
        row.deleted_at = utcnow() - dt.timedelta(days=TRASH_RETENTION_DAYS + 1)
        db.commit()
        assert purge_expired_trash(db) == 1
        assert db.query(Link).filter(Link.code == "stale").count() == 0

    def test_trash_days_left(self, client):
        create(client, code="dleft")
        client.delete("/api/links/dleft")
        data = client.get("/api/trash").json()
        assert data["retention_days"] == 30
        assert 28 <= data["items"][0]["days_left"] <= 30

    def test_guest_cannot_see_trash(self, anon_client):
        assert anon_client.get("/api/trash").status_code == 401

    def test_search_trash(self, client):
        create(client, code="alpha1", target="https://alpha.com/x")
        create(client, code="beta1", target="https://beta.com/x")
        client.delete("/api/links/alpha1")
        client.delete("/api/links/beta1")
        items = client.get("/api/trash?q=alpha").json()["items"]
        assert [i["code"] for i in items] == ["alpha1"]


class TestBulkExtras:
    """批量续期与批量打标签。勾一整页然后一个个改是很笨的事。"""

    def test_bulk_renew(self, client):
        create(client, code="rn1")
        create(client, code="rn2")
        r = client.post("/api/links/bulk", json={"action": "renew", "codes": ["rn1", "rn2"], "days": 90})
        assert r.json()["affected"] == 2
        for code in ("rn1", "rn2"):
            assert client.get(f"/api/links/{code}").json()["expires_at"] is not None

    def test_bulk_tag_dedupes(self, client):
        create(client, code="gt1", tags=["已有"])
        r = client.post("/api/links/bulk", json={"action": "tag", "codes": ["gt1"], "tag": "已有"})
        assert r.json()["affected"] == 1
        assert client.get("/api/links/gt1").json()["tags"] == ["已有"]
        client.post("/api/links/bulk", json={"action": "tag", "codes": ["gt1"], "tag": "新标签"})
        assert set(client.get("/api/links/gt1").json()["tags"]) == {"已有", "新标签"}

    def test_bulk_tag_requires_name(self, client):
        create(client, code="gt2")
        r = client.post("/api/links/bulk", json={"action": "tag", "codes": ["gt2"], "tag": "  "})
        assert r.status_code == 422


class TestExpiringNotify:
    def test_expiring_brief_lists_soon_to_die(self, client, db):
        from app.core.timeutil import utcnow

        create(
            client,
            code="soon",
            expires_at=(utcnow() + dt.timedelta(days=3)).isoformat(),
        )
        items = client.get("/api/admin/dashboard").json()["expiring"]
        assert any(item["code"] == "soon" for item in items)

    def test_push_without_channel_is_rejected(self, client):
        """没配通道就说清楚，别假装发过了。"""
        from app.core.timeutil import utcnow

        create(client, code="soon2", expires_at=(utcnow() + dt.timedelta(days=3)).isoformat())
        r = client.post("/api/admin/notify/expiring")
        assert r.status_code == 422
        assert "通道" in r.json()["message"]

    def test_push_with_no_expiring_links(self, client):
        r = client.post("/api/admin/notify/expiring")
        # 没有即将到期的链接时先短路，不要求配通道
        assert r.status_code == 200
        assert r.json()["count"] == 0


class TestBackup:
    def test_backup_refuses_memory_db(self, client):
        """内存库没有文件可下载，明确说清楚而不是给个坏 zip。"""
        r = client.get("/api/admin/backup")
        assert r.status_code == 422
        assert "备份" in r.json()["message"] or "库文件" in r.json()["message"]

    def test_backup_requires_admin(self, anon_client):
        assert anon_client.get("/api/admin/backup").status_code in (401, 403)
