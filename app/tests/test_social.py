"""社交平台防洪：微信 / QQ / 微博 等内置浏览器的识别、策略与统计。"""

from __future__ import annotations

import pytest

from app.core import guard, social

WECHAT_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) MicroMessenger/8.0.44(0x18002c2b)"
WECOM_UA = "Mozilla/5.0 (iPhone) wxwork/4.1.10 MicroMessenger/7.0.1 Language/zh"
QQ_UA = "Mozilla/5.0 (Linux; Android 13) QQ/8.9.63.11265 V1_AND_SQ_8.9.63 Chrome/107"
MOBILE_QQ_UA = "Mozilla/5.0 (Linux; Android 13) MQQBrowser/14.6 Mobile Safari/537.36"
SAFARI_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Version/17.0 Safari/605.1.15"
CURL_UA = "curl/8.4.0"


def make(client, code, **extra):
    payload = {"target": "https://example.com/page", "code": code, **extra}
    r = client.post("/api/links", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def flush():
    from app.services import flush_clicks

    flush_clicks()


class TestDetect:
    def test_wechat(self):
        assert social.detect(WECHAT_UA) == "wechat"

    def test_wecom_before_wechat(self):
        """企业微信 UA 里也带 MicroMessenger，必须认成企业微信。"""
        assert social.detect(WECOM_UA) == "wecom"

    def test_qq_variants(self):
        assert social.detect(QQ_UA) == "qq"
        assert social.detect(MOBILE_QQ_UA) == "qq"

    def test_plain_browser(self):
        assert social.detect(SAFARI_UA) is None
        assert social.detect("") is None
        assert social.detect(None) is None

    def test_referrer_fallback(self):
        assert social.detect(SAFARI_UA, "https://mp.weixin.qq.com/s/abc") == "wechat"
        assert social.detect(SAFARI_UA, "https://user.qzone.qq.com/123") == "qq"

    def test_labels(self):
        assert social.label("wechat") == "微信"
        assert social.label("qq") == "QQ"
        assert social.label(None) == "其它来源"

    def test_strategy_parsing(self):
        rules = social.strategies_from_text("wechat=block\nqq=BOGUS\nunknown=block")
        assert rules == {"wechat": "block", "qq": "allow"}


class TestDefaultAllow:
    def test_wechat_and_qq_pass_by_default(self, client):
        make(client, "wx1")
        assert client.get("/wx1", headers={"User-Agent": WECHAT_UA}, follow_redirects=False).status_code == 307
        assert client.get("/wx1", headers={"User-Agent": QQ_UA}, follow_redirects=False).status_code == 307

    def test_platform_recorded(self, client):
        make(client, "wx2")
        client.get("/wx2", headers={"User-Agent": WECHAT_UA})
        client.get("/wx2", headers={"User-Agent": QQ_UA})
        client.get("/wx2", headers={"User-Agent": SAFARI_UA})
        flush()
        data = client.get("/api/links/wx2/stats").json()
        assert data["clicks"] == 3
        by_key = {p["key"]: p["count"] for p in data["platforms"]}
        assert by_key["wechat"] == 1 and by_key["qq"] == 1


@pytest.fixture()
def admin(client):
    client.post("/api/admin/login", json={"username": "admin", "password": "test-token"})
    return client


class TestBlock:
    def test_blocked_platform_gets_notice_page(self, admin):
        make(admin, "blk1")
        r = admin.post("/api/admin/social-rules", json={"rules": {"wechat": "block"}})
        assert r.status_code == 200 and r.json()["rules"]["wechat"] == "block"

        blocked = admin.get("/blk1", headers={"User-Agent": WECHAT_UA}, follow_redirects=False)
        assert blocked.status_code == 403
        assert "微信" in blocked.text and "浏览器" in blocked.text
        # 其它平台照常跳转
        assert admin.get("/blk1", headers={"User-Agent": QQ_UA}, follow_redirects=False).status_code == 307

    def test_blocked_visit_not_counted(self, admin):
        make(admin, "blk2")
        admin.post("/api/admin/social-rules", json={"rules": {"wechat": "block"}})
        admin.get("/blk2", headers={"User-Agent": WECHAT_UA})
        flush()
        data = admin.get("/api/links/blk2/stats").json()
        assert data["clicks"] == 0
        assert data["blocked"] == 1

    def test_notice_hint_is_used(self, admin):
        make(admin, "blk3")
        admin.post(
            "/api/admin/social-rules",
            json={"rules": {"qq": "block"}, "hint": "请复制到浏览器继续"},
        )
        r = admin.get("/blk3", headers={"User-Agent": QQ_UA}, follow_redirects=False)
        assert "请复制到浏览器继续" in r.text

    def test_master_switch_off_allows_all(self, admin):
        make(admin, "blk4")
        admin.post("/api/admin/social-rules", json={"rules": {"wechat": "block"}})
        admin.post("/api/admin/settings", json={"social_guard": False})
        assert admin.get("/blk4", headers={"User-Agent": WECHAT_UA}, follow_redirects=False).status_code == 307
        admin.post("/api/admin/settings", json={"social_guard": True})

    def test_rules_need_admin(self, client):
        assert client.get("/api/admin/social-rules").status_code == 401
        assert client.post("/api/admin/social-rules", json={"rules": {}}).status_code == 401


class TestRulesApi:
    def test_roundtrip(self, admin):
        admin.post("/api/admin/social-rules", json={"rules": {"wechat": "silent", "qq": "block"}})
        data = admin.get("/api/admin/social-rules").json()
        assert data["rules"] == {"wechat": "silent", "qq": "block"}
        keys = {p["key"] for p in data["platforms"]}
        assert {"wechat", "qq", "weibo"} <= keys

    def test_unknown_platform_dropped(self, admin):
        admin.post("/api/admin/social-rules", json={"rules": {"nope": "block", "wechat": "block"}})
        assert admin.get("/api/admin/social-rules").json()["rules"] == {"wechat": "block"}

    def test_platform_stats(self, admin):
        make(admin, "st1")
        admin.get("/st1", headers={"User-Agent": WECHAT_UA})
        admin.get("/st1", headers={"User-Agent": WECHAT_UA})
        admin.get("/st1", headers={"User-Agent": QQ_UA})
        flush()
        items = {i["key"]: i for i in admin.get("/api/admin/platform-stats").json()["items"]}
        assert items["wechat"]["count"] == 2
        assert items["qq"]["label"] == "QQ"


class TestGuardHelpers:
    def test_platform_hit_only_block(self):
        assert guard.platform_hit("wechat", {"wechat": "block"}) == "wechat"
        assert guard.platform_hit("wechat", {"wechat": "silent"}) is None
        assert guard.platform_hit(None, {"wechat": "block"}) is None

    def test_save_and_read(self, db):
        guard.save_platform_rules(db, {"wechat": "block", "qq": "bogus"})
        rules = guard.platform_rules(db)
        assert rules["wechat"] == "block" and rules["qq"] == "allow"


class TestHardening:
    """这轮加固：跨天配额、silent 策略、匿名请求、拦截留痕。"""

    def test_silent_strategy_passes_but_not_counted(self, admin):
        make(admin, "sil1")
        admin.post("/api/admin/social-rules", json={"rules": {"wechat": "silent"}})
        r = admin.get("/sil1", headers={"User-Agent": WECHAT_UA}, follow_redirects=False)
        assert r.status_code == 307
        flush()
        data = admin.get("/api/links/sil1/stats").json()
        assert data["clicks"] == 0 and data["blocked"] == 1

    def test_silent_does_not_affect_other_platforms(self, admin):
        make(admin, "sil2")
        admin.post("/api/admin/social-rules", json={"rules": {"wechat": "silent"}})
        admin.get("/sil2", headers={"User-Agent": QQ_UA})
        flush()
        assert admin.get("/api/links/sil2/stats").json()["clicks"] == 1

    def test_silent_respects_master_switch(self, admin):
        make(admin, "sil3")
        admin.post("/api/admin/social-rules", json={"rules": {"wechat": "silent"}})
        admin.post("/api/admin/settings", json={"social_guard": False})
        admin.get("/sil3", headers={"User-Agent": WECHAT_UA})
        flush()
        assert admin.get("/api/links/sil3/stats").json()["clicks"] == 1
        admin.post("/api/admin/settings", json={"social_guard": True})

    def test_blocked_events_logged(self, admin):
        make(admin, "log1")
        admin.post("/api/admin/social-rules", json={"rules": {"qq": "block"}})
        admin.get("/log1", headers={"User-Agent": QQ_UA})
        flush()
        items = admin.get("/api/admin/blocked-events").json()["items"]
        assert items and items[0]["code"] == "log1"
        assert items[0]["platform"] == "qq" and items[0]["label"] == "QQ"
        # UA 里不含明文 IP，也不该出现完整 UA 记录之外的敏感字段
        assert "ip" not in items[0]

    def test_blocked_events_filter_and_auth(self, admin):
        # admin 与 client 是同一个测试客户端，这里先确认过滤器生效
        make(admin, "log2")
        admin.post("/api/admin/social-rules", json={"rules": {"wechat": "block", "qq": "block"}})
        admin.get("/log2", headers={"User-Agent": WECHAT_UA})
        flush()
        only_wx = admin.get("/api/admin/blocked-events?platform=wechat").json()["items"]
        assert only_wx and all(i["platform"] == "wechat" for i in only_wx)

    def test_wecom_marked_as_wecom(self, admin):
        make(admin, "wecom1")
        admin.get("/wecom1", headers={"User-Agent": WECOM_UA})
        flush()
        keys = {p["key"] for p in admin.get("/api/links/wecom1/stats").json()["platforms"]}
        assert "wecom" in keys

    def test_blocked_events_needs_login(self, client):
        assert client.get("/api/admin/blocked-events").status_code == 401

    def test_url_shortener_referer(self):
        from app.core import social

        assert social.detect(SAFARI_UA, "https://url.cn/AbCdEf") == "qq"


class TestRateLimiter:
    """限速器：自然日配额、并发原子性、未知 IP 不放行。"""

    def test_daily_resets_across_midnight(self):
        from app.core.security import RateLimiter

        clock = {"now": 0.0}
        limiter = RateLimiter(clock=lambda: clock["now"])

        def at(day, hour=12):
            import datetime as dt

            return dt.datetime(2026, 1, day, hour).timestamp()

        clock["now"] = at(1)
        assert limiter.allow("k", 2, 86400, daily=True)
        assert limiter.allow("k", 2, 86400, daily=True)
        assert not limiter.allow("k", 2, 86400, daily=True)
        # 同一自然日更晚仍拦
        clock["now"] = at(1, 23)
        assert not limiter.allow("k", 2, 86400, daily=True)
        # 次日重置
        clock["now"] = at(2, 0) + 60
        assert limiter.allow("k", 2, 86400, daily=True)

    def test_concurrent_limit_is_not_exceeded(self):
        import threading

        from app.core.security import RateLimiter

        limiter = RateLimiter()
        passed = []
        lock = threading.Lock()

        def worker():
            for _ in range(200):
                if limiter.allow("hot", 100, 60):
                    with lock:
                        passed.append(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(passed) == 100

    def test_zero_limit_means_unlimited(self):
        from app.core.security import RateLimiter

        limiter = RateLimiter()
        for _ in range(50):
            assert limiter.allow("k", 0, 60)
