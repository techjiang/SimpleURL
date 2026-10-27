"""每日额度与公开热门榜。

「每日上限」和站点级的 `click_daily_limit` 不是一回事：前者是这条链接的
活动预算（一共只放 N 个人进来，跟谁点的无关），后者是单 IP 防刷。
这两套账混在一起，调防刷就会顺手把活动预算也改了。

热门榜只给短码、标题和点击数——访客没有「全部链接」的视野，
把目标地址挂上去等于把私链变成公开目录。
"""

from __future__ import annotations

import datetime as dt

from app.core import settings_store
from app.core.clicks import click_buffer
from app.core.timeutil import utcnow


def make(client, **kwargs):
    payload = {"target": "https://example.com/a", **kwargs}
    r = client.post("/api/links", json=payload)
    assert r.status_code in (200, 201), r.text
    return r.json()


def hit(client, code: str):
    """走一次跳转（不跟随），返回响应。"""
    return client.get(f"/{code}", follow_redirects=False)


class TestDailyLimit:
    def test_default_is_unlimited(self, client):
        link = make(client, code="dlim1")
        assert link["daily_limit"] == 0
        assert link["daily_used"] == 0
        assert link["daily_exhausted"] is False

    def test_limit_is_stored_and_reported(self, client):
        link = make(client, code="dlim2", daily_limit=3)
        assert link["daily_limit"] == 3
        assert link["daily_exhausted"] is False

    def test_can_be_set_on_update(self, client):
        make(client, code="dlim3")
        r = client.patch("/api/links/dlim3", json={"daily_limit": 7})
        assert r.status_code == 200, r.text
        assert r.json()["daily_limit"] == 7

    def test_jump_works_until_exhausted(self, client):
        make(client, code="dlim4", daily_limit=2)
        assert hit(client, "dlim4").status_code == 307
        assert hit(client, "dlim4").status_code == 307

    def test_exhausted_link_returns_429(self, client):
        make(client, code="dlim5", daily_limit=1)
        assert hit(client, "dlim5").status_code == 307
        r = hit(client, "dlim5")
        assert r.status_code == 429
        assert "额度" in r.text

    def test_usage_is_visible_before_flush(self, client):
        """缓冲里那几票也要算进 daily_used。

        只读库的话，刚点完的那一瞬间列表还说「还剩 1 次」，
        下一次点击就撞上 429——数字和实际对不上，比不显示还糟。
        """
        make(client, code="dlim6", daily_limit=5)
        hit(client, "dlim6")
        detail = client.get("/api/links/dlim6").json()
        assert detail["daily_used"] == 1

    def test_usage_survives_flush(self, client, flush_clicks):
        make(client, code="dlim7", daily_limit=5)
        hit(client, "dlim7")
        flush_clicks()
        detail = client.get("/api/links/dlim7").json()
        assert detail["daily_used"] == 1
        assert detail["daily_exhausted"] is False

    def test_exhausted_flag_after_flush(self, client, flush_clicks):
        make(client, code="dlim8", daily_limit=1)
        hit(client, "dlim8")
        flush_clicks()
        assert client.get("/api/links/dlim8").json()["daily_exhausted"] is True

    def test_blocked_click_is_recorded_as_daily_reason(self, client, db):
        """额度用尽的访问要留痕，不然「今天为什么少了一堆访问」没法解释。"""
        from sqlalchemy import select

        from app.models import ClickEvent

        make(client, code="dlim9", daily_limit=1)
        hit(client, "dlim9")
        hit(client, "dlim9")
        click_buffer.flush()
        reasons = db.execute(select(ClickEvent.block_reason).where(ClickEvent.block_reason == "daily")).all()
        assert reasons, "额度用尽的访问没有留痕"

    def test_yesterday_usage_counts_as_zero(self, client, db):
        """日切靠比对日期，不靠定时任务清零。"""
        from sqlalchemy import select

        from app.models import Link

        make(client, code="dlima", daily_limit=2)
        row = db.execute(select(Link).where(Link.code == "dlima")).scalar_one()
        row.daily_used = 2
        row.daily_date = (utcnow() - dt.timedelta(days=1)).date()
        db.commit()

        assert hit(client, "dlima").status_code == 307

    def test_today_usage_still_counts(self, client, db):
        from sqlalchemy import select

        from app.models import Link

        make(client, code="dlimb", daily_limit=2)
        row = db.execute(select(Link).where(Link.code == "dlimb")).scalar_one()
        row.daily_used = 2
        row.daily_date = utcnow().date()
        db.commit()

        assert hit(client, "dlimb").status_code == 429

    def test_negative_limit_rejected(self, client):
        r = client.post("/api/links", json={"target": "https://example.com/b", "daily_limit": -1})
        assert r.status_code == 422

    def test_no_limit_never_exhausts(self, client):
        make(client, code="dlimc")
        for _ in range(5):
            assert hit(client, "dlimc").status_code == 307

    def test_buffer_reset_clears_daily_pending(self, client):
        """缓冲清空时额度占用也要清，否则那条链接会一直「还剩 0 次」。"""
        make(client, code="dlimd", daily_limit=3)
        hit(client, "dlimd")
        assert click_buffer.pending_daily(
            client.get("/api/links/dlimd").json()["id"]
        ) >= 0
        click_buffer.reset()
        assert click_buffer.pending_daily(client.get("/api/links/dlimd").json()["id"]) == 0


class TestBoard:
    def test_empty_board_is_empty_list(self, client):
        assert client.get("/api/board").json()["items"] == []

    def test_board_lists_clicked_links(self, client, flush_clicks):
        make(client, code="brd1", title="热门一号")
        for _ in range(3):
            hit(client, "brd1")
        flush_clicks()

        items = client.get("/api/board").json()["items"]
        codes = [item["code"] for item in items]
        assert "brd1" in codes

    def test_board_never_exposes_target(self, client, flush_clicks):
        make(client, code="brd2", title="别泄露")
        hit(client, "brd2")
        flush_clicks()

        items = client.get("/api/board").json()["items"]
        item = next(i for i in items if i["code"] == "brd2")
        assert "target" not in item
        assert "https://example.com" not in str(item)

    def test_board_skips_links_without_clicks(self, client):
        make(client, code="brd3")
        codes = [i["code"] for i in client.get("/api/board").json()["items"]]
        assert "brd3" not in codes

    def test_board_skips_disabled(self, client, flush_clicks):
        make(client, code="brd4")
        hit(client, "brd4")
        flush_clicks()
        client.patch("/api/links/brd4", json={"enabled": False})

        codes = [i["code"] for i in client.get("/api/board").json()["items"]]
        assert "brd4" not in codes

    def test_board_skips_deleted(self, client, flush_clicks):
        make(client, code="brd5")
        hit(client, "brd5")
        flush_clicks()
        client.delete("/api/links/brd5")

        codes = [i["code"] for i in client.get("/api/board").json()["items"]]
        assert "brd5" not in codes

    def test_board_sorted_by_clicks(self, client, flush_clicks):
        make(client, code="brda")
        make(client, code="brdb")
        hit(client, "brda")
        for _ in range(3):
            hit(client, "brdb")
        flush_clicks()

        items = client.get("/api/board").json()["items"]
        codes = [i["code"] for i in items]
        assert codes.index("brdb") < codes.index("brda")

    def test_board_size_zero_disables(self, client, db, flush_clicks):
        make(client, code="brdc")
        hit(client, "brdc")
        flush_clicks()
        settings_store.set_value(db, "board_size", 0)
        db.commit()
        assert client.get("/api/board").json()["items"] == []

    def test_board_respects_size_setting(self, client, db, flush_clicks):
        for idx in range(4):
            make(client, code=f"brdz{idx}")
            hit(client, f"brdz{idx}")
        flush_clicks()
        settings_store.set_value(db, "board_size", 2)
        db.commit()
        assert len(client.get("/api/board").json()["items"]) <= 2


class TestBoardPage:
    def test_home_renders_board_slot(self, client):
        page = client.get("/").text
        assert "board" in page

    def test_board_endpoint_in_api_docs(self, client):
        assert "/api/board" in client.get("/tools").text


class TestFrontendWiring:
    """前端静态断言：这些点一旦漏掉，页面不报错但功能是哑的。"""

    def test_index_has_daily_limit_field(self):
        from pathlib import Path

        html = (Path(__file__).resolve().parents[1] / "templates" / "index.html").read_text(encoding="utf-8")
        assert 'id="daily_limit"' in html
        assert 'id="edit-daily"' in html

    def test_index_has_hotboard_block(self):
        from pathlib import Path

        html = (Path(__file__).resolve().parents[1] / "templates" / "index.html").read_text(encoding="utf-8")
        assert 'id="hotboard"' in html
        assert 'id="hotboard-list"' in html

    def test_app_js_sends_daily_limit(self):
        from pathlib import Path

        js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        assert "daily_limit: intOrUndefined" in js
        assert "function loadBoard" in js

    def test_admin_js_shows_quota(self):
        from pathlib import Path

        js = (Path(__file__).resolve().parents[1] / "static" / "admin.js").read_text(encoding="utf-8")
        assert "function quotaMark" in js
        assert "额度用尽" in js

    def test_css_has_board_styles(self):
        from pathlib import Path

        css = (Path(__file__).resolve().parents[1] / "static" / "app.css").read_text(encoding="utf-8")
        assert ".hotboard" in css
        assert ".quota-mark" in css
        assert ".badge.warn" in css
