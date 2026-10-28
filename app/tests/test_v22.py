"""v22：唯一访客不再缩水、复用短码不再作废别人的回执、今日口径按本地日。

这轮挑的都是「跑起来不报错、数字自己慢慢变」的类型：

1. 独立访客原来靠 `count(distinct ip_hash)` 现算，而点击明细按保留期清理
   （默认 180 天）。清完之后「总点击」不变、「独立访客」一格一格往下掉，
   同一个页面上两个数字各说各话。现在单独一张 `link_visitors` 记谁来过。
2. 创建接口带 `reuse: true`（或短码撞车自动换码）会把现成那条还回去，
   顺手又签了一张新回执 —— 第一位创建者手里那张当场作废。他做错了什么？
   什么都没做。
3. 仪表盘「今日」取的是 `now.astimezone().date()`，也就是**系统**时区。
   容器里系统跑 UTC、应用配 `TZ=Asia/Shanghai` 是最常见的一套组合，
   于是本地 00:00~08:00 之间后台的「今日点击」全程挂在昨天。
4. 统计页的二维码会带着 cookie 里那张回执去请求，而那张可能属于别的链接，
   于是图裂了 —— 页面上看就是「图坏了」，没人猜得到是回执串了链接。
5. 链接自己的每日额度按 UTC 日切，页面文案却写「明天零点恢复」，
   东八区用户的额度会在早上 8 点忽然回满。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.core.clicks import click_buffer
from app.core.timeutil import today_local

ADMIN_SRC = (Path(__file__).resolve().parents[1] / "admin_api.py").read_text(encoding="utf-8")


def make(client, code, **kwargs):
    payload = {"target": f"https://example.com/{code}", "code": code, **kwargs}
    r = client.post("/api/links", json=payload)
    assert r.status_code in (200, 201), r.text
    return r.json()


class TestUniqueVisitors:
    def test_pending_visitor_counts_immediately(self, client):
        """缓冲里那几票也算进访客数：点完立刻看统计不该是 0。"""
        code = "uv1"
        make(client, code)
        from app.core.db import session_scope
        from app.models import Link

        with session_scope() as db:
            link_id = db.execute(select(Link.id).where(Link.code == code)).scalar_one()
        click_buffer.add(None, link_id, None, "Mozilla/5.0 (Macintosh)", "203.0.113.9")
        assert click_buffer.pending_visitors(link_id) == 1
        # 同一个 IP 再来一次不算新访客
        click_buffer.add(None, link_id, None, "Mozilla/5.0 (Macintosh)", "203.0.113.9")
        assert click_buffer.pending_visitors(link_id) == 1

    def test_same_visitor_counts_once(self, client, flush_clicks):
        make(client, "uv2")
        for _ in range(3):
            client.get("/uv2", follow_redirects=False)
        flush_clicks()
        data = client.get("/api/links/uv2/stats").json()
        assert data["clicks"] == 3
        assert data["unique_visitors"] == 1

    def test_visitor_row_is_written(self, client, flush_clicks):
        """落库时真的往 link_visitors 写一行，日期是本地自然日。"""
        from app.core.db import session_scope
        from app.models import LinkVisitor

        make(client, "uv3")
        client.get("/uv3", follow_redirects=False)
        flush_clicks()
        with session_scope() as db:
            rows = db.execute(select(LinkVisitor)).scalars().all()
            assert len(rows) == 1
            assert rows[0].day == today_local()

    def test_pruning_click_events_keeps_unique_visitors(self, client, flush_clicks):
        """清掉明细之后「独立访客」不能跟着缩水——这就是这轮改动的理由。"""
        from app.core.db import session_scope
        from app.models import ClickEvent

        make(client, "uv4")
        client.get("/uv4", follow_redirects=False)
        flush_clicks()
        with session_scope() as db:
            db.execute(delete(ClickEvent))
        data = client.get("/api/links/uv4/stats").json()
        assert data["clicks"] == 1
        assert data["unique_visitors"] == 1

    def test_second_visitor_counts_twice(self, client, flush_clicks):
        from app.core.db import session_scope
        from app.models import LinkVisitor

        make(client, "uv5")
        client.get("/uv5", follow_redirects=False)
        flush_clicks()
        with session_scope() as db:
            link_id = db.execute(select(LinkVisitor.link_id)).scalars().first()
            db.add(LinkVisitor(link_id=link_id, day=today_local(), ip_hash="d" * 32))
        assert client.get("/api/links/uv5/stats").json()["unique_visitors"] == 2

    def test_delete_purges_visitor_rows(self, client, flush_clicks):
        """彻底删除时缓存里的访客记录要一起丢掉，别落到下一条同号链接头上。"""
        from app.core.db import session_scope
        from app.models import LinkVisitor

        make(client, "uv6")
        client.get("/uv6", follow_redirects=False)
        flush_clicks()
        client.delete("/api/links/uv6")
        client.delete("/api/trash/uv6")
        with session_scope() as db:
            assert db.execute(select(LinkVisitor)).scalars().all() == []


class TestReuseKeepsReceipt:
    @pytest.fixture()
    def guest(self, anon_client):
        return anon_client

    def test_reuse_does_not_revoke_existing_receipt(self, guest):
        """复用同一条链接时不能重签：否则第一位的回执凭空作废。"""
        from app.core.claim import verify
        from app.core.db import session_scope
        from app.core.middleware import CLAIM_COOKIE
        from app.models import Link

        first = make(guest, "rc1")
        token = first["claim_token"]
        assert token

        again = guest.post("/api/links", json={"target": "https://example.com/rc1", "reuse": True}).json()
        assert again["code"] == "rc1"
        assert again["reused"] is True
        # 明文拿不回来是正常的（库里只有哈希），但旧那张必须仍然有效
        assert again["claim_token"] is None
        assert guest.cookies.get(CLAIM_COOKIE) == token

        with session_scope() as db:
            link = db.execute(select(Link).where(Link.code == "rc1")).scalar_one()
            assert verify(db, link, token) is True

    def test_reuse_on_link_without_receipt_issues_one(self, guest):
        """管理员建的链接没有回执；访客复用拿走时补发一张。"""
        from app.core.db import session_scope
        from app.models import Link

        with session_scope() as db:
            db.add(Link(code="rc2", target="https://example.com/rc2"))
        r = guest.post("/api/links", json={"target": "https://example.com/rc2", "reuse": True})
        assert r.status_code in (200, 201), r.text
        assert r.json()["claim_token"]

    def test_swapped_code_gets_fresh_receipt(self, guest):
        """短码被占自动换码时是新链接，必须发一张新回执。"""
        make(guest, "rc3")
        r = guest.post("/api/links", json={"target": "https://example.com/rc3-b", "code": "rc3"})
        assert r.status_code in (200, 201), r.text
        data = r.json()
        assert data["code"] != "rc3"
        assert data["swapped_from"] == "rc3"
        assert data["claim_token"]


class TestTodayIsLocalDay:
    def test_dashboard_source_uses_today_local(self):
        assert "today = today_local()" in ADMIN_SRC
        assert "now.astimezone().date()" not in ADMIN_SRC.replace("`now.astimezone().date()`", "")

    def test_today_local_matches_configured_zone(self, monkeypatch):
        import time as _time

        monkeypatch.setenv("TZ", "Asia/Shanghai")
        try:
            _time.tzset()
        except AttributeError:
            pytest.skip("tzset 不可用")
        expected = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
        assert today_local() == expected


class TestDailyLimitUsesLocalDay:
    def test_used_today_follows_local_date(self):
        from app.models import Link

        link = Link(code="x", target="https://example.com/x", daily_limit=5)
        link.daily_date = today_local()
        link.daily_used = 3
        assert link.daily_used_today == 3
        link.daily_date = today_local() - dt.timedelta(days=1)
        assert link.daily_used_today == 0

    def test_flush_writes_local_date(self, client, flush_clicks):
        from app.core.db import session_scope
        from app.models import Link

        make(client, "dl22", daily_limit=3)
        client.get("/dl22", follow_redirects=False)
        flush_clicks()
        with session_scope() as db:
            row = db.execute(select(Link).where(Link.code == "dl22")).scalar_one()
            assert row.daily_date == today_local()
            assert row.daily_used == 1


class TestStatsQrClaimIsScoped:
    def test_foreign_claim_is_not_used_for_qr(self, client, anon_client):
        """cookie 里的回执若不属于这条链接，统计页的二维码不能带上它。"""
        from app.core.middleware import CLAIM_COOKIE

        # 访客建一条，回执落进 anon 的 cookie；再建一条把 cookie 换成第二条的
        make(anon_client, "qr1")
        make(anon_client, "qr2")
        token = anon_client.cookies.get(CLAIM_COOKIE)
        assert token

        # 管理员打开 qr1 的统计页：能看，但那张 qr2 的回执不该被拼进图片地址
        page = client.get("/s/qr1").text
        assert f"t={token}" not in page
        assert "/api/qr/qr1" in page

    def test_own_claim_still_used(self, anon_client):
        from app.core.middleware import CLAIM_COOKIE

        make(anon_client, "qr3")
        token = anon_client.cookies.get(CLAIM_COOKIE)
        page = anon_client.get(f"/s/qr3?t={token}").text
        assert f"t={token}" in page


class TestStatCardGrid:
    """统计页五张卡片要一行排满，不能「4 + 1」掉一张到下一行。"""

    def test_fixed_columns(self):
        css = (Path(__file__).resolve().parents[1] / "static" / "app.css").read_text(encoding="utf-8")
        assert ".stat-grid { display: grid; gap: 14px; grid-template-columns: repeat(5, minmax(0, 1fr))" in css
        # auto-fit 自动填充会在某些宽度下排出 4+1，右边空一大片
        assert "repeat(auto-fit, minmax(170px, 1fr))" not in css

    def test_has_narrow_breakpoints(self):
        css = (Path(__file__).resolve().parents[1] / "static" / "app.css").read_text(encoding="utf-8")
        assert "@media (max-width: 860px)" in css
