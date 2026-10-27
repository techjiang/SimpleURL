"""每日额度、批量操作与公开热门榜。"""

from __future__ import annotations

from app.core.clickbuffer import click_buffer


def _create(client, **kwargs):
    payload = {"target": "https://example.com/daily"}
    payload.update(kwargs)
    res = client.post("/api/links", json=payload)
    assert res.status_code == 201, res.text
    return res.json()


def test_daily_limit_blocks_after_quota(client):
    link = _create(client, daily_clicks_limit=2, title="每日两次")
    assert link["daily_clicks_limit"] == 2
    assert link["daily_used"] == 0

    for _ in range(2):
        assert client.get(f"/{link['code']}", follow_redirects=False).status_code == 307

    # 第三次被挡在门外，且不再计数
    res = client.get(f"/{link['code']}", follow_redirects=False)
    assert res.status_code == 410
    assert "今日" in res.text

    detail = client.get(f"/api/links/{link['code']}").json()
    assert detail["daily_used"] == 2
    assert detail["daily_exhausted"] is True
    assert detail["state"] == "daily"
    # 明细还没落库，累计点击数仍是 0；结算后补上
    click_buffer.flush()
    assert client.get(f"/api/links/{link['code']}").json()["clicks"] == 2


def test_daily_limit_resets_on_next_day(client):
    link = _create(client, daily_clicks_limit=1)
    assert client.get(f"/{link['code']}", follow_redirects=False).status_code == 307
    assert client.get(f"/{link['code']}", follow_redirects=False).status_code == 410

    # 把日期改到昨天，等于跨天——不需要任何定时任务
    from app.core.db import session_scope
    from app.core.timeutil import utcnow
    from app.models import Link
    import datetime as dt

    yesterday = (utcnow() - dt.timedelta(days=1)).date().isoformat()
    with session_scope() as db:
        row = db.query(Link).filter(Link.code == link["code"]).one()
        row.daily_date = yesterday
    click_buffer.reset()

    assert client.get(f"/{link['code']}", follow_redirects=False).status_code == 307
    assert client.get(f"/api/links/{link['code']}").json()["daily_used"] == 1


def test_clear_daily_limit(client):
    link = _create(client, daily_clicks_limit=1)
    res = client.patch(f"/api/links/{link['code']}", json={"clear_daily_limit": True})
    assert res.status_code == 200
    assert res.json()["daily_clicks_limit"] is None
    for _ in range(3):
        assert client.get(f"/{link['code']}", follow_redirects=False).status_code == 307


def test_bulk_enable_disable_delete(client):
    a = _create(client, target="https://example.com/a")
    b = _create(client, target="https://example.com/b")

    res = client.post("/api/links/bulk", json={"codes": [a["code"], b["code"]], "action": "disable"})
    body = res.json()
    assert res.status_code == 200 and body["done"] == 2
    assert client.get(f"/{a['code']}", follow_redirects=False).status_code == 410

    client.post("/api/links/bulk", json={"codes": [a["code"]], "action": "enable"})
    assert client.get(f"/{a['code']}", follow_redirects=False).status_code == 307

    res = client.post("/api/links/bulk", json={"codes": [a["code"], b["code"]], "action": "delete"})
    assert res.json()["done"] == 2
    assert client.get(f"/api/links/{a['code']}").status_code == 404


def test_bulk_reports_missing_and_rejects_bad_action(client):
    code = _create(client)["code"]
    body = client.post("/api/links/bulk", json={"codes": [code, "ghost"], "action": "enable"}).json()
    assert body["done"] == 1
    assert body["missing"] == ["ghost"]

    assert client.post("/api/links/bulk", json={"codes": [code], "action": "rename"}).status_code == 422
    assert client.post("/api/links/bulk", json={"codes": [], "action": "enable"}).status_code == 400


def test_bulk_requires_admin(guest):
    assert guest.post("/api/links/bulk", json={"codes": ["abc"], "action": "delete"}).status_code == 401


def test_board_is_public_but_hides_targets(client, guest):
    link = _create(client, target="https://example.com/board-target", title="榜上有名")
    client.get(f"/{link['code']}", follow_redirects=False)
    click_buffer.flush()

    body = guest.get("/api/board").json()
    assert body["items"], "有点击的链接应该上榜"
    item = body["items"][0]
    assert item["code"] == link["code"]
    assert item["title"] == "榜上有名"
    assert item["clicks"] == 1
    assert "target" not in item, "访客不该看到目标地址"


def test_board_hides_disabled_links(client, guest):
    link = _create(client, target="https://example.com/gone")
    client.get(f"/{link['code']}", follow_redirects=False)
    click_buffer.flush()
    client.patch(f"/api/links/{link['code']}", json={"enabled": False})
    assert guest.get("/api/board").json()["items"] == []


def test_daily_quota_counts_buffered_clicks(client):
    """额度按「落库 + 缓冲」一起算，否则同一秒的并发会一起看到「还剩 1 次」。"""
    link = _create(client, daily_clicks_limit=3)

    assert client.get(f"/{link['code']}", follow_redirects=False).status_code == 307
    # 还没 flush，但后台读到的今日已用应该是 1，而不是 0
    detail = client.get(f"/api/links/{link['code']}").json()
    assert detail["daily_used"] == 1

    for _ in range(2):
        assert client.get(f"/{link['code']}", follow_redirects=False).status_code == 307
    assert client.get(f"/{link['code']}", follow_redirects=False).status_code == 410

    click_buffer.flush()
    detail = client.get(f"/api/links/{link['code']}").json()
    assert detail["daily_used"] == 3
    assert detail["clicks"] == 3


def test_stats_expose_today_clicks(client):
    link = _create(client, target="https://example.com/stats-today")
    client.get(f"/{link['code']}", follow_redirects=False, headers={"referer": "https://example.com/x"})
    click_buffer.flush()
    stats = client.get(f"/api/links/{link['code']}/stats").json()
    assert stats["clicks_today"] == 1


def test_stats_page_flushes_buffer(client):
    """统计页与统计接口都先结算缓冲，刚点过的链不该显示 0。"""
    link = _create(client, target="https://example.com/flush-stats")
    client.get(f"/{link['code']}", follow_redirects=False, headers={"referer": "https://cnb.cool/"})

    stats = client.get(f"/api/links/{link['code']}/stats").json()
    assert stats["clicks"] == 1
    assert stats["clicks_today"] == 1
    assert stats["top_referrers"][0]["source"] == "cnb.cool"

    client.get(f"/{link['code']}", follow_redirects=False)
    page = client.get(f"/s/{link['code']}")
    assert page.status_code == 200 and "2" in page.text
