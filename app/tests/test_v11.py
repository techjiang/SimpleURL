"""v11 专项：点击去哪儿了，得说得清。

这轮四件事，都是「数字对不上但界面上看不出问题」那一类：

1. 超出每日配额的访问以前连留痕都没有——跳转照常 307，库里一行不落。
   于是「今天明明有人访问，点击却比昨天还少」谁也解释不了。
2. `blocked` 一个布尔装了四种原因（UA / 平台拦截 / 放行不计数 / 配额），
   后台只能显示「已拦截」，该调阈值还是该改规则全靠猜。
3. 趋势图的窗口按「现在往前推 29 天」算，本地 08:00 之后今天那根柱子
   永远是空的：卡片上写着今天 23 次点击，图上一片平。
4. 图没有纵轴刻度，1 次和 100 次是同一种东西。
"""

from __future__ import annotations

import datetime as dt

from app.core.timeutil import local_day_start, today_local
from app.services import NO_CLICK_SOCIAL_SILENT, NO_CLICK_UA, NO_CLICK_QUOTA

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/131.0 Safari/537.36"


def make(client, code, target="https://example.com/page"):
    r = client.post("/api/links", json={"target": target, "code": code, "fetch_title": False})
    assert r.status_code == 201, r.text
    return r.json()


class TestQuotaIsVisible:
    """超配额的访问要留痕，不能凭空消失。"""

    def test_over_quota_still_leaves_a_trace(self, client, flush_clicks):
        make(client, "q1x")
        assert client.post("/api/admin/settings", json={"click_daily_limit": 2}).status_code == 200
        for _ in range(5):
            assert client.get("/q1x", headers={"user-agent": UA}, follow_redirects=False).status_code == 307
        flush_clicks()

        # 跳转全部照旧放行，一个 307 没少
        stats = client.get("/api/links/q1x/stats").json()
        assert stats["clicks"] == 2, "配额内的两次才计入点击"
        reasons = {row["reason"]: row["count"] for row in stats["no_click"]}
        assert reasons.get(NO_CLICK_QUOTA) == 3, "超出配额的三次必须留痕"

    def test_quota_split_is_listed_in_dashboard_and_runtime(self, client, flush_clicks):
        make(client, "q2x")
        client.post("/api/admin/settings", json={"click_daily_limit": 1})
        for _ in range(4):
            client.get("/q2x", headers={"user-agent": UA}, follow_redirects=False)
        flush_clicks()

        dash = client.get("/api/admin/dashboard").json()
        by_reason = {row["reason"]: row["count"] for row in dash["no_click"]}
        assert by_reason[NO_CLICK_QUOTA] == 3
        assert dash["blocked_events"] == 3
        assert dash["total_clicks"] == 1

        runtime = client.get("/api/admin/runtime").json()
        assert runtime["no_click"][NO_CLICK_QUOTA] == 3

    def test_quota_abuse_raises_a_warning(self, client, flush_clicks):
        """配额用尽的比例高了，运行状态要直说要调阈值。"""
        make(client, "q3x")
        client.post("/api/admin/settings", json={"click_daily_limit": 5})
        for _ in range(30):
            client.get("/q3x", headers={"user-agent": UA}, follow_redirects=False)
        flush_clicks()
        warns = client.get("/api/admin/runtime").json()["warnings"]
        assert any("每日点击配额" in w["text"] for w in warns), warns


class TestReasonsAreSeparated:
    """四种「没计数」的原因必须分得开。"""

    def test_ua_and_silent_are_different_reasons(self, client, flush_clicks):
        make(client, "r1x")
        client.post("/api/admin/ua-rules", json={"rules": "BadBot"})
        client.post("/api/admin/social-rules", json={"rules": {"wechat": "silent"}})

        client.get("/r1x", headers={"user-agent": "BadBot/1.0"}, follow_redirects=False)
        client.get(
            "/r1x",
            headers={"user-agent": "Mozilla/5.0 (iPhone) MicroMessenger/8.0"},
            follow_redirects=False,
        )
        flush_clicks()

        stats = client.get("/api/links/r1x/stats").json()
        assert stats["clicks"] == 0
        reasons = {row["reason"]: row["count"] for row in stats["no_click"]}
        assert reasons.get(NO_CLICK_UA) == 1
        assert reasons.get(NO_CLICK_SOCIAL_SILENT) == 1
        # silent 才是「放行不计数」，UA 屏蔽不该被算进去
        assert stats["silent"] == 1

    def test_blocked_events_carry_reason_and_filter(self, client, flush_clicks):
        make(client, "r2x")
        client.post("/api/admin/ua-rules", json={"rules": "BadBot"})
        client.get("/r2x", headers={"user-agent": "BadBot/1.0"}, follow_redirects=False)
        flush_clicks()

        items = client.get("/api/admin/blocked-events").json()["items"]
        assert items and items[0]["reason"] == NO_CLICK_UA
        assert items[0]["reason_label"] == "UA 屏蔽"

        only_quota = client.get(f"/api/admin/blocked-events?reason={NO_CLICK_QUOTA}").json()["items"]
        assert only_quota == []


def select_one_link():
    """取当前唯一的链接 id：造点击时不能假设自增 id 一定是 1。"""
    from sqlalchemy import select

    from app.models import Link

    return select(Link.id)


def _FrozenClock(fake_utc):
    """假时钟：把 dt.datetime.now() 固定住。"""

    class _Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_utc if tz else fake_utc.replace(tzinfo=None)

    return _Clock


class TestChartWindow:
    """趋势窗口按本地自然日切，今天必须有一根柱子。"""

    def test_window_includes_today(self, client, flush_clicks):
        make(client, "c1x")
        client.get("/c1x", headers={"user-agent": UA}, follow_redirects=False)
        flush_clicks()

        daily = client.get("/api/links/c1x/stats?days=30").json()["daily"]
        assert len(daily) == 30
        assert daily[-1]["date"] == today_local().isoformat(), "最后一根必须是今天"
        assert daily[-1]["count"] == 1

    def test_window_starts_at_local_midnight(self):
        """窗口起点必须是「本地 00:00」，不是「现在往前推 N 天」。

        这不是纯粹的代码洁癖：本地 01:00 就是 UTC 前一天 17:00，
        按「现在往前推 29 天」一算，起点落在今天 00:00 之后 ——
        今天全天的点击一根柱子都没有，卡片上却明写着有。
        这里直接把两种算法摆在一起比，不依赖跑测试的时刻。
        """
        for days in (7, 14, 30):
            today = today_local()
            first = today - dt.timedelta(days=days - 1)
            # 假设此刻是本地凌晨 01:00
            fake_now = dt.datetime.combine(today, dt.time(1, 0)).astimezone(dt.timezone.utc)
            naive = fake_now - dt.timedelta(days=days - 1)
            right = local_day_start(first)
            # 「现在往前推」算出来比本地零点还晚，今天 00:00 到那一刻的点击就被漏在窗口外
            assert naive > right, "现在往前推的起点晚于本地零点，今天开头那段点击会被漏掉"

            assert right.astimezone().strftime("%H:%M") == "00:00"
            assert right.astimezone().date() == first

    def test_local_day_start_is_local_midnight(self):
        day = dt.date(2026, 10, 25)
        start = local_day_start(day)
        assert start.tzinfo is not None
        # 换算到本地就是那天的 00:00，而不是 UTC 00:00（本地早上 8 点）
        assert start.astimezone().strftime("%Y-%m-%d %H:%M") == "2026-10-25 00:00"


class TestAxisTicks:
    """前端刻度算法：装在 app.js 里，这里用 node 跑一遍确保步长好看、上限包住数据。"""

    def test_axis_ticks_are_nice(self):
        import json
        import shutil
        import subprocess

        if not shutil.which("node"):
            return  # 没装 node 的 CI 跳过，别让一条前端用例卡住后端流水线
        script = """
        const fs = require('fs');
        const src = fs.readFileSync('app/static/app.js', 'utf8');
        const m = src.match(/function axisTicks\\([\\s\\S]*?\\n  }/);
        const b = src.match(/function buildTicks\\([\\s\\S]*?\\n  }/);
        eval(m[0] + '\\n' + (b ? b[0] : ''));
        console.log(JSON.stringify([axisTicks(0), axisTicks(3), axisTicks(47), axisTicks(1e5)]));
        """
        out = subprocess.run(
            ["node", "-e", script], cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2]),
            capture_output=True, text=True, check=True,
        ).stdout
        zero, three, mid, big = json.loads(out)
        for item in (zero, three, mid, big):
            assert item["ticks"][0] == 0
            assert item["ticks"][-1] == item["top"]
            steps = {b - a for a, b in zip(item["ticks"], item["ticks"][1:])}
            assert len(steps) == 1, "刻度要等距"
            assert list(steps)[0] in {1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000,
                                      20000, 50000, 100000}
        assert three["top"] >= 3 and mid["top"] >= 47 and big["top"] >= 1e5


def test_both_charts_share_axis_algorithm():
    """前台和后台的刻度必须同一套算法。

    两份实现各自演进过一次：统计页已经改成四段步长，后台还停在三段，
    于是同一个数字在两个页面上画出来的高度不同。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    app_js = (root / "app/static/app.js").read_text(encoding="utf-8")
    admin_js = (root / "app/static/admin.js").read_text(encoding="utf-8")
    assert "count = 4" in app_js and "count = 4" in admin_js
    # 旧的「四舍五入取步长」写法在两处都不能再出现：峰值 55 会被画成满格
    assert "Math.round((norm <= 1" not in app_js
    assert "Math.round((norm <= 1" not in admin_js
