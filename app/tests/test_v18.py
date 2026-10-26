"""v18：延迟计数的可见性、彻底删除后的孤儿点击、CSV 整行中和、图表无障碍。

这轮挑的都是「功能都在、测试全绿，但数字会莫名对不上」的那类问题。
每一条都先在真实调用路径上复现，再钉住。
"""

from __future__ import annotations

import datetime as dt
import time

import pytest

from app.core.timeutil import local_day_start, today_local


def _create(client, target, **kw):
    r = client.post("/api/links", json={"target": target, **kw})
    assert r.status_code == 201, r.text
    return r.json()


# ---------- 1. 热点链接把延迟计数打回原形 ----------


class TestDelayedFlush:
    """定时器是「多久检查一次」，不该等于「多久落一次库」。"""

    def test_click_becomes_visible_quickly(self, client):
        """点完一秒内去看，数字就应该已经在库里了。

        只靠 `CLICK_FLUSH_INTERVAL`（默认 5 秒）落库的话，中间那几秒
        用户刷新列表看到的是 0，还得自己理解「待结算」是什么意思。
        """
        link = _create(client, "https://example.com/hot")
        client.get("/" + link["code"])
        from app.core.clicks import click_buffer

        deadline = time.time() + 1.5
        while time.time() < deadline and click_buffer.pending() > 0:
            time.sleep(0.05)
        assert click_buffer.pending() == 0
        assert client.get(f"/api/links/{link['code']}").json()["clicks"] == 1

    def test_blocked_hit_does_not_schedule_flush(self):
        """被拦下的访问不排落库：否则刷量的人每来一次就唤醒一次写库。

        直接问缓冲区，不走 HTTP —— 这条要钉的是「谁有资格唤醒写库」，
        不是跳转逻辑本身。
        """
        from app.core.clicks import click_buffer

        click_buffer.reset()
        click_buffer.start(3600)  # 只开「稍后落库」，周期拉到很长免得干扰
        try:
            click_buffer.add(None, 1, None, "curl/8", "1.2.3.4", reason="ua", schedule=False)
            assert click_buffer.pending() == 1
            assert click_buffer._delay_timer is None

            click_buffer.add(None, 1, None, "curl/8", "1.2.3.4")
            assert click_buffer._delay_timer is not None
        finally:
            click_buffer.stop()
            click_buffer.reset()

    def test_concurrent_flush_does_not_double_count(self, client):
        """多个 flush 撞在一起也不能把同一批算两遍。"""
        import threading

        link = _create(client, "https://example.com/concurrent")
        from app.core.clicks import click_buffer

        for _ in range(20):
            client.get("/" + link["code"])
        threads = [threading.Thread(target=click_buffer.flush) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert click_buffer.pending() == 0
        assert client.get(f"/api/links/{link['code']}").json()["clicks"] == 20


# ---------- 2. 彻底删除之后，缓冲里的点击成了孤儿 ----------


class TestDiscardOnPurge:
    def test_pending_click_does_not_leak_to_next_link(self, client):
        """删掉带未落库点击的链接，不能让下一次新建的链接「自带」点击数。

        SQLite 的 rowid 会复用被删掉的号：老链接的点击还压在缓冲里，
        新链接拿回同一个 id，flush 之后这几次点击就算到新人头上了。
        """
        from app.core.clicks import click_buffer

        old = _create(client, "https://example.com/orphan")
        client.get("/" + old["code"])
        assert click_buffer.pending() >= 1

        client.delete(f"/api/links/{old['code']}")
        assert client.delete(f"/api/trash/{old['code']}").status_code == 200

        new = _create(client, "https://example.com/brand-new")
        click_buffer.flush()
        assert client.get(f"/api/links/{new['code']}").json()["clicks"] == 0

    def test_discard_returns_dropped_count(self):
        """discard 得说清丢了几条，日志里才有据可查。"""
        from app.core.clicks import click_buffer

        click_buffer.reset()
        click_buffer.add(None, 42, None, "curl/8", "1.2.3.4")
        click_buffer.add(None, 42, None, "curl/8", "1.2.3.4")
        click_buffer.add(None, 43, None, "curl/8", "1.2.3.4")
        assert click_buffer.discard(42) == 2
        assert click_buffer.pending() == 1
        click_buffer.reset()


# ---------- 3. CSV 导出的公式注入 ----------


class TestCsvSanitizing:
    def test_every_cell_is_neutralized(self, client):
        """整行统一中和，而不是逐格手写——漏一格等于没做。

        标题是访客能填的字段，漏了它，管理员把 CSV 发给同事、
        对方双击打开，`=HYPERLINK(...)` 就在本地跑起来了。
        """
        link = _create(
            client,
            "https://example.com/formula",
            title="=HYPERLINK(\"http://evil.example\",\"点我\")",
            note="+1+1",
            tags=["@tag"],
            fetch_title=False,
        )
        body = client.get("/api/links/export").text
        assert "'=HYPERLINK" in body
        assert "'+1+1" in body
        assert "'@tag" in body
        # 原始的危险写法一个都不能留
        assert "\n=HYPERLINK" not in body.replace("\r\n", "\n")
        assert f"/{link['code']}" in body

    def test_negative_numbers_stay_readable(self, client):
        """纯数字不拦：`-5` 前缀单引号在 Excel 里就成了文本，反而更难用。"""
        from app.services import _safe_cell

        assert _safe_cell(-5) == -5
        assert _safe_cell("-5") == "'-5"


# ---------- 4. 趋势图的无障碍入口 ----------


def test_chart_table_is_open_by_default(client):
    """图表是脚本画的，读屏和「关闭 JS」的人都只能靠那张明细表。

    它默认折着，等于对这群人不可达。
    """
    link = _create(client, "https://example.com/a11y")
    html = client.get(f"/s/{link['code']}").text
    assert 'class="chart-table" open' in html


# ---------- 5. 跨夏令时的按日分组 ----------


class TestLocalDayBucketing:
    """一条固定的 UTC 偏移套一年，夏令时地区每年会错一个月。"""

    def test_offset_is_per_day(self, monkeypatch):
        """换季那两天前后，同一条 UTC 时间要落到不同的本地日期。"""
        from app.core import timeutil

        monkeypatch.setenv("TZ", "America/New_York")
        time.tzset()
        try:
            # 纽约 2026-03-08 02:00 由 -5 变 -4：UTC 07:30 在换季前后差一小时
            before = timeutil._offset_sql_at(dt.datetime(2026, 3, 8, 6, 30, tzinfo=dt.timezone.utc))
            after = timeutil._offset_sql_at(dt.datetime(2026, 3, 9, 6, 30, tzinfo=dt.timezone.utc))
            assert before != after
            assert before == "-5 hours" and after == "-4 hours"
        finally:
            time.tzset()

    def test_buckets_use_local_midnight(self, client, monkeypatch):
        """本地零点刚过的点击，要算在「今天」这一根柱子上。"""
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        time.tzset()
        link = _create(client, "https://example.com/bucket")
        from app.core.clicks import click_buffer
        from app.core.db import session_scope
        from app.models import ClickEvent, Link

        # 本地刚刚过零点（UTC 还是前一天 16:00）
        today = today_local()
        moment = local_day_start(today) + dt.timedelta(minutes=1)
        with session_scope() as db:
            row = db.query(Link).filter(Link.code == link["code"]).one()
            db.add(ClickEvent(link_id=row.id, ts=moment, device="desktop", blocked=False))
            row.clicks = (row.clicks or 0) + 1
        click_buffer.reset()
        data = client.get(f"/api/links/{link['code']}/stats").json()
        assert data["daily"][-1]["date"] == today.isoformat()
        assert data["daily"][-1]["count"] >= 1


# ---------- 6. 短码撞车改成「换码」 ----------


class TestCodeSwap:
    def test_swapped_code_is_used_and_reported(self, client):
        first = _create(client, "https://example.com/one", code="Take")
        r = client.post(
            "/api/links",
            json={"target": "https://example.com/two", "code": "Take", "fetch_title": False},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["swapped_from"] == "Take"
        assert body["code"] != "Take"
        assert client.get("/api/links/Take").json()["code"] == first["code"]

    def test_auto_code_still_works_without_custom(self, client):
        link = _create(client, "https://example.com/auto")
        assert link["swapped_from"] in (None, "")
        assert len(link["code"]) >= 3


# ---------- 7. 限速：上一分钟的账不该记到下一位访客头上 ----------


class TestRateLimitWindow:
    def test_next_minute_starts_clean(self, monkeypatch):
        """有人把这一分钟刷满之后，下一分钟的访客必须放行。

        旧写法限速 key 是个常量，bucket 里那条满额记录没人清——
        「最近 60 秒」算出来的永远是满额，于是第 61 秒来的正常访客照样被拒。
        """
        from app.core import guard
        from app.core.security import rate_limiter

        rate_limiter.reset()
        clock = [1_700_000_000.0]  # 固定在一个整分钟上
        monkeypatch.setattr(guard.time, "time", lambda: clock[0])
        rate_limiter._clock = lambda: clock[0]

        ip = "203.0.113.9"
        allowed = [guard.check_redirect_quota(None, ip, {"redirect_rate_limit": 3})[0] for _ in range(3)]
        assert all(allowed)
        # 配额用尽，本分钟最后一位被拒
        assert guard.check_redirect_quota(None, ip, {"redirect_rate_limit": 3})[0] is False

        # 跨到下一分钟：正常访客应当立刻放行，不用等满 60 秒
        clock[0] += 61
        assert guard.check_redirect_quota(None, ip, {"redirect_rate_limit": 3})[0] is True
        rate_limiter.reset()
        rate_limiter._clock = __import__("time").time

    def test_unknown_ip_still_shares_quota(self):
        """拿不到 IP 时退化成一整份配额，不是直接放行。"""
        from app.core import guard
        from app.core.security import rate_limiter

        rate_limiter.reset()
        assert guard.check_redirect_quota(None, None, {"redirect_rate_limit": 1})[0] is True
        assert guard.check_redirect_quota(None, None, {"redirect_rate_limit": 1})[0] is False
        rate_limiter.reset()


def test_axis_keeps_peak_off_the_ceiling():
    """峰值落在刻度上限上时，最高的柱子会顶满，看着像「还有更多」。

    55 次和 60 次画出来一样高，只有 hover 才知道差 5。
    """
    import json
    import shutil
    import subprocess
    from pathlib import Path

    if not shutil.which("node"):
        return
    root = Path(__file__).resolve().parents[2]
    script = """
    const fs = require('fs');
    const src = fs.readFileSync('app/static/app.js', 'utf8');
    eval(src.match(/function axisTicks\\([\\s\\S]*?\\n  }/)[0] + '\\n' + src.match(/function buildTicks\\([\\s\\S]*?\\n  }/)[0]);
    console.log(JSON.stringify([axisTicks(55), axisTicks(58), axisTicks(19)]));
    """
    out = subprocess.run(["node", "-e", script], cwd=str(root), capture_output=True, text=True, check=True).stdout
    a, b, c = json.loads(out)
    assert a["top"] > 55, "55 次不该顶到刻度的最上面"
    assert b["top"] > 58
    assert c["top"] >= 19
