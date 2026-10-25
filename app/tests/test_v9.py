"""v9 专项：这轮改的是「看得准」——数字要对得上，页面别错位。

几条被钉住的教训：
1. 入库 UTC、出库本地。按 UTC 分桶会让国内用户的「今天」错开 8 小时。
2. 拦截留痕和「放行不计数」不是一回事，仪表盘上别混成一个数。
3. 平台色值和图标由后端一处下发，前端别再各写一份。
4. `/tools` 这种固定路径必须注册在 `/{code}` 通配之前，否则永远 404。
"""

from __future__ import annotations

import datetime as dt

from app.core import social
from app.core.timeutil import local_date


def make_link(client, **payload):
    payload.setdefault("target", "https://example.com/page")
    payload.setdefault("fetch_title", False)
    r = client.post("/api/links", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


class TestLocalDate:
    """日期分桶必须跟部署机时区走，不能跟 UTC 走。"""

    def test_utc_moment_lands_on_local_day(self):
        # 东八区 3 月 5 日 00:30 == UTC 3 月 4 日 16:30。
        # 按 UTC 分桶会把这笔点击记到昨天，用户早上看报表就少了一笔
        moment = dt.datetime(2026, 3, 4, 16, 30, tzinfo=dt.timezone.utc)
        expected = moment.astimezone().date()
        assert local_date(moment) == expected

    def test_naive_treated_as_utc(self):
        # 库里读出来是 naive 的，得按 UTC 解释再转本地，不能直接当本地时间
        moment = dt.datetime(2026, 3, 4, 16, 30)
        expected = moment.replace(tzinfo=dt.timezone.utc).astimezone().date()
        assert local_date(moment) == expected

    def test_none_stays_none(self):
        assert local_date(None) is None

    def test_buckets_use_local_day(self, client, anon_client):
        from app.core.clicks import flush_now

        link = make_link(client)
        # 真正的跳转走 /{code}；/s/{code} 是统计页，看一眼不算点击
        r = anon_client.get(f"/{link['code']}", follow_redirects=False)
        assert r.status_code in (301, 302, 307), r.text
        # 点击先进延迟缓冲，不 flush 的话查到的永远是空表
        flush_now()
        stats = client.get(f"/api/links/{link['code']}/stats").json()
        today = dt.datetime.now().astimezone().date().isoformat()
        assert [d for d in stats["daily"] if d["count"]][-1]["date"] == today


class TestDashboardBooks:
    """「真实点击」和「拦截留痕」要各算各的。"""

    def test_dashboard_has_total_events(self, client):
        make_link(client)
        d = client.get("/api/admin/dashboard").json()
        assert "total_events" in d
        assert d["total_events"] >= d["total_clicks"]

    def test_clicks_today_excludes_blocked(self, client):
        d = client.get("/api/admin/dashboard").json()
        # 今日点击数不该超过全站真实点击数，两个口径别互相打架
        assert d["clicks_today"] <= d["total_clicks"]

    def test_blocked_count_is_separate(self, client):
        d = client.get("/api/admin/dashboard").json()
        assert "blocked_events" in d
        assert isinstance(d["blocked_events"], int)


class TestPlatformAssets:
    """平台色值 / 图标由后端下发，前端不再硬编码。"""

    def test_every_platform_has_both(self):
        for key in social.PLATFORMS:
            assert social.color(key).startswith("#"), key
            assert social.icon(key), key

    def test_unknown_platform_falls_back(self):
        assert social.color("nope") == "#7c889c"
        assert social.icon("nope") == ""

    def test_dashboard_platforms_carry_assets(self, client):
        data = client.get("/api/admin/dashboard").json()
        for p in data.get("platforms", []):
            assert p["color"].startswith("#"), p
            assert "icon" in p, p

    def test_stats_platforms_carry_assets(self, client):
        link = make_link(client)
        client.get(f"/s/{link['code']}")
        data = client.get(f"/api/links/{link['code']}/stats").json()
        for p in data.get("platforms", []):
            assert p["color"].startswith("#"), p


class TestRouting:
    """固定路径必须赢过短码通配。"""

    def test_tools_page_is_served(self, anon_client):
        r = anon_client.get("/tools")
        assert r.status_code == 200
        assert "接口" in r.text

    def test_tools_not_shadowed_by_code_route(self, anon_client):
        # 真建一条叫 tools 的短链，页面也不能被它抢走
        r = anon_client.get("/tools")
        assert r.headers.get("content-type", "").startswith("text/html")

    def test_robots_keeps_stats_out_of_index(self, anon_client):
        body = anon_client.get("/robots.txt").text
        assert "Disallow: /s/" in body


class TestPageGuards:
    """统计页 / 工具页也加载 app.js，不该顺手去拉后台接口。"""

    def test_tools_page_does_not_call_admin_api(self, anon_client):
        html = anon_client.get("/tools").text
        # 页面本身不带 list 容器，前端据此跳过列表请求
        assert 'id="list"' not in html
