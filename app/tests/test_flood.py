"""防洪：限速、UA 屏蔽、微信 / QQ 内打开。"""

from __future__ import annotations

import pytest

from app.core import guard, runtime
from app.core.clickbuffer import click_buffer


def make(client, code="fl1", target="https://example.com/page"):
    r = client.post("/api/links", json={"target": target, "code": code})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def tune():
    """改运行配置并保证用例结束后复位。"""

    def _set(**values):
        runtime.set_many(values)

    yield _set
    runtime.reset()
    guard.flood.reset()


class TestRateLimit:
    def test_per_ip_minute_blocks_with_429(self, client, tune):
        make(client, "fl2")
        tune(guard_ip_per_minute=2, guard_block_seconds=30)
        guard.flood.reset()
        assert client.get("/fl2", follow_redirects=False).status_code == 307
        assert client.get("/fl2", follow_redirects=False).status_code == 307
        r = client.get("/fl2", follow_redirects=False)
        assert r.status_code == 429
        assert "访问过于频繁" in r.text or "先歇一会儿" in r.text
        assert r.headers["Retry-After"] == "30"

    def test_zero_means_unlimited(self, client, tune):
        make(client, "fl3")
        tune(guard_ip_per_minute=0)
        guard.flood.reset()
        for _ in range(30):
            assert client.get("/fl3", follow_redirects=False).status_code == 307

    def test_code_level_limit(self, client, tune):
        make(client, "fl4")
        tune(guard_ip_per_minute=0, guard_code_per_minute=2)
        guard.flood.reset()
        client.get("/fl4")
        client.get("/fl4")
        assert client.get("/fl4", follow_redirects=False).status_code == 429

    def test_overflow_is_logged(self, client, tune):
        make(client, "fl5")
        tune(guard_ip_per_minute=1)
        guard.flood.reset()
        client.get("/fl5")
        client.get("/fl5")
        events = client.get("/api/admin/overview").json()["events"]
        assert any(e["kind"] == "rate" for e in events)


class TestUaBlock:
    def test_blocked_ua_redirects_but_not_counted(self, client, tune):
        make(client, "ua1")
        tune(blocked_uas="*bot*")
        guard.flood.reset()
        r = client.get("/ua1", headers={"User-Agent": "Mozilla/5.0 EvilBot/2.0"}, follow_redirects=False)
        # 命中屏蔽仍然放行跳转，只是不计入统计
        assert r.status_code == 200  # 中转页（提示用户）而非 307
        click_buffer.flush()
        detail = client.get("/api/links/ua1").json()
        assert detail["clicks"] == 0

    def test_normal_ua_counts(self, client, tune):
        make(client, "ua2")
        tune(blocked_uas="*bot*")
        guard.flood.reset()
        r = client.get("/ua2", headers={"User-Agent": "Mozilla/5.0 (Macintosh)"}, follow_redirects=False)
        assert r.status_code == 307
        click_buffer.flush()
        assert client.get("/api/links/ua2").json()["clicks"] == 1

    def test_ua_match_helpers(self):
        assert guard.match_ua("Googlebot/2.1", ["*bot*"]) == "*bot*"
        assert guard.match_ua("curl/8.0", ["curl/"]) == "curl/"
        assert guard.match_ua("Mozilla", ["*bot*"]) is None
        assert guard.match_ua(None, ["*bot*"]) is None

    def test_disabled_switch(self, client, tune):
        make(client, "ua3")
        tune(blocked_uas="*bot*", blocked_ua_enabled=False)
        guard.flood.reset()
        r = client.get("/ua3", headers={"User-Agent": "EvilBot"}, follow_redirects=False)
        assert r.status_code == 307


class TestWechatQQ:
    WECHAT_UA = "Mozilla/5.0 (iPhone) MicroMessenger/8.0.42 NetType/WIFI"
    QQ_UA = "Mozilla/5.0 (Linux; Android) QQ/8.9.33.10335"

    def test_wechat_warn_shows_interstitial(self, client, tune):
        make(client, "wx1")
        tune(wechat_mode="warn", guard_ip_per_minute=0)
        guard.flood.reset()
        r = client.get("/wx1", headers={"User-Agent": self.WECHAT_UA}, follow_redirects=False)
        assert r.status_code == 200
        assert "微信" in r.text
        assert "复制链接" in r.text
        # 中转页里带着真实目标，用户点「继续访问」能走
        assert "https://example.com/page" in r.text

    def test_wechat_warn_still_counts(self, client, tune):
        make(client, "wx2")
        tune(wechat_mode="warn")
        guard.flood.reset()
        client.get("/wx2", headers={"User-Agent": self.WECHAT_UA})
        click_buffer.flush()
        assert client.get("/api/links/wx2").json()["clicks"] == 1

    def test_wechat_block_returns_403(self, client, tune):
        make(client, "wx3")
        tune(wechat_mode="blocked")
        guard.flood.reset()
        r = client.get("/wx3", headers={"User-Agent": self.WECHAT_UA}, follow_redirects=False)
        assert r.status_code == 403
        assert "复制链接" in r.text

    def test_wechat_off_passes_through(self, client, tune):
        make(client, "wx4")
        tune(wechat_mode="off")
        guard.flood.reset()
        r = client.get("/wx4", headers={"User-Agent": self.WECHAT_UA}, follow_redirects=False)
        assert r.status_code == 307

    def test_qq_detected(self, client, tune):
        make(client, "qq1")
        tune(qq_mode="warn")
        guard.flood.reset()
        r = client.get("/qq1", headers={"User-Agent": self.QQ_UA}, follow_redirects=False)
        assert r.status_code == 200
        assert "QQ" in r.text

    def test_wechat_takes_priority_over_qq(self):
        # 微信 UA 里常带 QQ 浏览器内核标记，不能被判成 QQ
        assert guard.is_wechat(self.WECHAT_UA) is True
        assert guard.is_qq(self.WECHAT_UA) is False
        assert guard.is_qq(self.QQ_UA) is True


class TestClickBuffer:
    def test_batch_flush_writes_once(self, client, tune):
        make(client, "cb1")
        tune(guard_ip_per_minute=0)
        guard.flood.reset()
        for _ in range(5):
            client.get("/cb1")
        assert click_buffer.pending >= 5
        written = click_buffer.flush()
        assert written >= 5
        assert click_buffer.pending == 0
        assert client.get("/api/links/cb1").json()["clicks"] == 5

    def test_flush_endpoint(self, client):
        make(client, "cb2")
        client.get("/cb2")
        r = client.post("/api/admin/flush")
        assert r.status_code == 200
        assert r.json()["written"] >= 1
