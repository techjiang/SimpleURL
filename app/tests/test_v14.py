"""v14：把「提交了才报错」「配了没生效」「填了看不见」这三类收干净。

这一轮每条用例都对得上一个能复现的具体现象，不是「写个断言证明代码能跑」：

1. 设「12 月 31 日 23:59 过期」，实际却在北京时间 1 月 1 日 07:59 才失效
2. 自作聪明换个短码，把站内 `/tools` 盖掉
3. 反代后面所有访客共用一个 IP，限速与配额形同虚设
4. 标签里带个引号，点进去永远是空列表
5. 备注建的时候能填，建完哪里都看不到、也改不掉
6. 告警冷却时长写死在代码里，运营调不了
"""

from __future__ import annotations

import datetime as dt
import time


class TestExpiryTimezone:
    """过期时间：浏览器给的是本地时间，服务端不能当 UTC 收。"""

    def test_local_input_is_converted_from_process_zone(self, monkeypatch):
        from app.core import timeutil

        monkeypatch.setenv("TZ", "Asia/Shanghai")
        time.tzset()
        try:
            got = timeutil.from_local_input(dt.datetime(2026, 12, 31, 23, 59))
        finally:
            monkeypatch.delenv("TZ", raising=False)
            time.tzset()
        # 北京时间 23:59 == UTC 15:59 同一天
        assert got == dt.datetime(2026, 12, 31, 15, 59, tzinfo=dt.timezone.utc)

    def test_aware_input_is_respected(self):
        from app.core.timeutil import from_local_input

        aware = dt.datetime(2026, 6, 1, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        assert from_local_input(aware) == dt.datetime(2026, 6, 1, 0, 0, tzinfo=dt.timezone.utc)

    def test_none_stays_none(self):
        from app.core.timeutil import from_local_input

        assert from_local_input(None) is None

    def test_expiry_roundtrip_through_api(self, client, monkeypatch):
        """填进去的本地时间，查回来还得是同一个时刻（不是同一个字符串）。"""
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        time.tzset()
        try:
            r = client.post(
                "/api/links",
                json={
                    "target": "https://example.com/tz",
                    "code": "tztest",
                    # 前端现在会补偏移，模拟真实浏览器
                    "expires_at": "2026-12-31T23:59:00+08:00",
                    "fetch_title": False,
                },
            )
            assert r.status_code == 201, r.text
            body = r.json()
            got = dt.datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
            assert got == dt.datetime(2026, 12, 31, 15, 59, tzinfo=dt.timezone.utc)
            # 本地时间显示回来仍然是 23:59，不是 07:59
            from app.core.timeutil import fmt_local

            assert fmt_local(got) == "2026-12-31 23:59"
        finally:
            monkeypatch.delenv("TZ", raising=False)
            time.tzset()

    def test_past_expiry_still_rejected(self, client):
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        r = client.post("/api/links", json={"target": "https://example.com/p", "expires_at": past})
        assert r.status_code == 422


class TestReservedPaths:
    """保留字只该有一份清单，而且必须覆盖站内真实存在的路径。"""

    def test_tools_and_yourls_are_reserved(self, client):
        for code in ("tools", "yourls-api", "Tools", "YOURls-API"):
            r = client.post("/api/links", json={"target": "https://example.com/x", "code": code})
            assert r.status_code == 422, f"{code} 应该被拒，实际 {r.status_code}"
            assert "保留字" in r.json()["message"]

    def test_tools_page_still_reachable(self, client):
        client.post("/api/links", json={"target": "https://example.com/x", "code": "tools"})
        r = client.get("/tools", follow_redirects=False)
        assert r.status_code == 200
        assert "接口" in r.text

    def test_reserved_set_is_single_source(self):
        """建链校验与页面路由读同一份清单，别再各写一份。"""
        from app.core.codes import reserved_conflict
        from app.pages import RESERVED_PATHS

        for name in RESERVED_PATHS:
            assert reserved_conflict(name), f"{name} 在 RESERVED_PATHS 里却没被拦住"

    def test_hint_price_check_uses_same_rule(self, client):
        r = client.get("/api/codes/tools/available")
        assert r.json()["available"] is False


class TestForwardedFor:
    """X-Forwarded-For 取最右侧非信任地址。"""

    def _req(self, header: str):
        class Client:
            host = "127.0.0.1"

        class Req:
            client = Client()
            headers = {"x-forwarded-for": header}
            cookies = {}

        return Req()

    def test_spoofed_leftmost_is_ignored(self, monkeypatch):
        from app.core.config import settings
        from app.core.security import client_ip

        monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.1")
        # 攻击者想让自己「每次都换个 IP」：左边随便填，右边是代理追加的真实地址
        assert client_ip(self._req("9.9.9.9, 8.8.8.8, 203.0.113.5")) == "203.0.113.5"

    def test_rate_limit_counts_real_client(self, client, monkeypatch):
        """限速按真实客户端计，伪造左侧地址不能刷出干净配额。"""
        from app.core.config import settings
        from app.core.guard import reset_guard

        monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.1")
        reset_guard()
        client.post("/api/links", json={"target": "https://example.com/r", "code": "xff1", "fetch_title": False})
        settings.redirect_rate_limit = 2
        try:
            codes = []
            for i in range(4):
                r = client.get(
                    "/xff1",
                    headers={"x-forwarded-for": f"7.7.7.{i}, 203.0.113.7"},
                    follow_redirects=False,
                )
                codes.append(r.status_code)
            # 同一个真实客户端，第 3 次起应该被限速；换最左边那个假 IP 不顶用
            assert codes.count(307) == 2, codes
            assert codes[-1] == 429, codes
        finally:
            settings.redirect_rate_limit = 120
            reset_guard()


class TestTagFilter:
    """标签名里有引号 / 反斜杠时也要筛得出来。"""

    def test_quoted_tag_is_filterable(self, client):
        r = client.post("/api/links", json={"target": "https://example.com/q1", "tags": ['a"b']})
        assert r.status_code == 201
        got = client.get("/api/links", params={"tag": 'a"b'}).json()
        assert [i["tags"] for i in got["items"]] == [['a"b']]

    def test_backslash_tag_is_filterable(self, client):
        client.post("/api/links", json={"target": "https://example.com/q2", "tags": ["x\\y"]})
        got = client.get("/api/links", params={"tag": "x\\y"}).json()
        assert got["total"] == 1

    def test_plain_tag_not_matched_by_prefix(self, client):
        """普通标签的行为不能因为多了个 or 就放宽：`a` 不该命中 `aa`。"""
        client.post("/api/links", json={"target": "https://example.com/q3", "tags": ["a"]})
        client.post("/api/links", json={"target": "https://example.com/q4", "tags": ["aa"]})
        got = client.get("/api/links", params={"tag": "a"}).json()
        assert got["total"] == 1
        assert got["items"][0]["tags"] == ["a"]

    def test_export_respects_quoted_tag(self, client):
        client.post("/api/links", json={"target": "https://example.com/q5", "tags": ['k"v']})
        text = client.get("/api/links/export", params={"tag": 'k"v'}).text
        assert "q5" in text or "k\"v" in text


class TestNote:
    """备注：建的时候能填，建完看得到、也改得掉。"""

    def test_note_visible_in_list_and_export(self, client):
        r = client.post(
            "/api/links",
            json={"target": "https://example.com/n1", "code": "note1", "note": "双十一活动页", "fetch_title": False},
        )
        assert r.status_code == 201
        listed = client.get("/api/links", params={"q": "note1"}).json()["items"][0]
        assert listed["note"] == "双十一活动页"

        csv_text = client.get("/api/links/export").text
        header = csv_text.splitlines()[0].lstrip("\ufeff")
        assert "备注" in header
        assert "双十一活动页" in csv_text

    def test_note_editable_by_admin(self, client):
        client.post("/api/links", json={"target": "https://example.com/n2", "code": "note2", "fetch_title": False})
        r = client.patch("/api/links/note2", json={"note": "改过的备注"})
        assert r.status_code == 200
        assert r.json()["note"] == "改过的备注"

    def test_note_editable_by_owner_receipt(self, client):
        created = client.post(
            "/api/links",
            json={"target": "https://example.com/n3", "code": "note3", "note": "初始", "fetch_title": False},
        ).json()
        token = created["claim_token"]
        r = client.patch(f"/api/links/note3/self?t={token}", json={"note": "访客也能改"})
        assert r.status_code == 200, r.text
        assert r.json()["note"] == "访客也能改"

    def test_csv_short_url_column_is_not_bare_code(self, client):
        client.post("/api/links", json={"target": "https://example.com/n4", "code": "note4", "fetch_title": False})
        line = next(l for l in client.get("/api/links/export").text.splitlines() if "note4" in l)
        # 短码列 + 短链接列，后者不该还是裸短码
        assert "/note4" in line


class TestNotifyCooldown:
    """告警冷却时长可调，且默认值有边界。"""

    def test_editable_and_clamped(self, client):
        from app.core.settings_store import EDITABLE, _cast

        assert "notify_cooldown" in EDITABLE
        assert _cast("notify_cooldown", "600", 3600) == 600
        assert _cast("notify_cooldown", "999999", 3600) == 86400
        assert _cast("notify_cooldown", "乱七八糟", 3600) == 3600

    def test_saved_value_takes_effect(self, client):
        from app.core.db import session_scope
        from app.core.settings_store import effective, set_value

        with session_scope() as db:
            set_value(db, "notify_cooldown", 90)
        with session_scope() as db:
            assert effective(db, "notify_cooldown") == 90

    def test_cooldown_suppresses_repeat(self, client, monkeypatch):
        """同一句告警在冷却期内只发一次——告警通道最怕变成噪音。"""
        from app.core import notify
        from app.main import _notify_warnings_once

        sent: list[str] = []
        monkeypatch.setattr(notify, "enabled", lambda db: ["webhook"])
        monkeypatch.setattr(notify, "send", lambda db, ch, title, body, level="info": sent.append(title) or {"ok": True})
        monkeypatch.setattr(notify, "digest_body", lambda warnings, db=None: "body")
        client.post("/api/admin/settings", json={"notify_enabled": True, "notify_cooldown": 3600})
        # 用例环境里没有真实告警，直接给一条，免得依赖「恰好存在黄灯」
        monkeypatch.setattr(
            "app.services.runtime_status",
            lambda db: {"warnings": [{"level": "warn", "text": "同一句告警"}]},
        )

        seen: dict[str, float] = {}
        _notify_warnings_once(seen)
        _notify_warnings_once(seen)
        assert len(sent) == 1, sent

    def test_cooldown_zero_always_sends(self, client, monkeypatch):
        from app.core import notify
        from app.main import _notify_warnings_once

        sent: list[str] = []
        monkeypatch.setattr(notify, "enabled", lambda db: ["webhook"])
        monkeypatch.setattr(notify, "send", lambda db, ch, title, body, level="info": sent.append(title) or {"ok": True})
        monkeypatch.setattr(notify, "digest_body", lambda warnings, db=None: "body")
        client.post("/api/admin/settings", json={"notify_enabled": True, "notify_cooldown": 0})
        monkeypatch.setattr(
            "app.services.runtime_status",
            lambda db: {"warnings": [{"level": "warn", "text": "每次都发"}]},
        )
        seen: dict[str, float] = {}
        _notify_warnings_once(seen)
        _notify_warnings_once(seen)
        assert len(sent) == 2
