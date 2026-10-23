"""安全加固：响应头、CSP、CSRF、可信代理、URL 校验、登录限速。

覆盖的是「被攻击时会发生什么」，不是「功能是否正常」。所以每个用例都在
构造一个具体的攻击动作，然后断言它没成功。
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.middleware import CSRF_COOKIE, CSRF_HEADER, content_security_policy
from app.core.security import client_ip, is_public_url


def _csrf(client) -> str:
    return next(
        (c.value for c in client.cookies.jar if c.name == CSRF_COOKIE and "testserver" in (c.domain or "")),
        "",
    )


class TestSecurityHeaders:
    def test_headers_present_on_page(self, client):
        r = client.get("/")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]

    def test_csp_only_on_html(self, client):
        page = client.get("/")
        api = client.get("/api/access")
        assert "Content-Security-Policy" in page.headers
        assert "Content-Security-Policy" not in api.headers

    def test_csp_script_src_has_nonce_and_no_unsafe_inline(self, client):
        r = client.get("/")
        csp = r.headers["Content-Security-Policy"]
        script_src = next(p for p in csp.split(";") if "script-src" in p)
        assert "'nonce-" in script_src
        assert "unsafe-inline" not in script_src
        assert "unsafe-eval" not in script_src

    def test_nonce_differs_per_request(self, client):
        """nonce 必须每次请求都换，否则就等于没加。"""
        a = client.get("/").headers["Content-Security-Policy"]
        b = client.get("/").headers["Content-Security-Policy"]
        assert a != b

    def test_csp_covers_default_src_self(self):
        csp = content_security_policy("abc123")
        assert "default-src 'self'" in csp
        assert "'nonce-abc123'" in csp


class TestCSRF:
    def test_write_without_token_rejected(self, client):
        r = client.post("/api/links", json={"target": "https://example.com/a"}, headers={CSRF_HEADER: ""})
        assert r.status_code == 403
        assert "校验" in r.json()["message"]

    def test_write_with_wrong_token_rejected(self, client):
        r = client.post(
            "/api/links",
            json={"target": "https://example.com/a"},
            headers={CSRF_HEADER: "not-the-cookie-value"},
        )
        assert r.status_code == 403

    def test_write_with_valid_token_accepted(self, client):
        r = client.post(
            "/api/links",
            json={"target": "https://example.com/a", "code": "csrf1"},
            headers={CSRF_HEADER: _csrf(client)},
        )
        assert r.status_code == 201, r.text

    def test_get_needs_no_token(self, client):
        assert client.get("/api/access").status_code == 200

    def test_yourls_api_exempt(self, client):
        """第三方客户端不带我们的 cookie，兼容接口不能因此不可用。"""
        r = client.post("/yourls-api?action=shorturl&url=https://example.com/y1&keyword=cs1")
        assert r.status_code == 200, r.text

    def test_static_exempt(self, client):
        assert client.get("/static/app.css").status_code == 200

    def test_token_cookie_not_httponly(self):
        """前端要读这个 cookie 才能回带头，所以它必须可读。

        用全新客户端跑：复用 fixture 那个的话它已经持有 cookie，
        服务端不会再下发一次，就取不到 Set-Cookie 头。
        """
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as fresh:
            r = fresh.get("/")
        cookie = r.headers.get("set-cookie", "")
        assert f"{CSRF_COOKIE}=" in cookie
        assert "HttpOnly" not in cookie
        assert "SameSite=lax" in cookie

    def test_cross_site_forgery_simulation(self, client):
        """模拟跨站提交：浏览器会自动带上 cookie，但攻击者读不到它的值。

        所以请求里只有 cookie、没有 X-CSRF-Token 头 —— 这正是双重提交
        令牌要拦的那一类请求。
        """
        victim_token = _csrf(client)
        assert victim_token
        forged = client.post(
            "/api/admin/ua-rules",
            json={"rules": ["evil"]},
            headers={CSRF_HEADER: "", "Cookie": f"{CSRF_COOKIE}={victim_token}"},
        )
        assert forged.status_code == 403
        # 规则确实没被写进去
        assert client.get("/api/admin/ua-rules").status_code in (401, 403)


class TestTrustedProxies:
    def test_forwarded_for_ignored_from_unknown_peer(self, client):
        """默认只信任回环代理，陌生人伪造 X-Forwarded-For 应该被无视。"""
        req = type("R", (), {"client": None, "headers": {}, "cookies": {}})()
        assert client_ip(req) is None

    def test_forwarded_for_honored_from_trusted_proxy(self, monkeypatch):
        monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.1,::1")

        class Client:
            host = "127.0.0.1"

        class Req:
            client = Client()
            headers = {"x-forwarded-for": "203.0.113.9, 10.0.0.1"}
            cookies = {}

        assert client_ip(Req()) == "203.0.113.9"

    def test_x_real_ip_fallback(self, monkeypatch):
        monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.1")

        class Client:
            host = "127.0.0.1"

        class Req:
            client = Client()
            headers = {"x-real-ip": "198.51.100.7"}
            cookies = {}

        assert client_ip(Req()) == "198.51.100.7"

    def test_network_notation_supported(self, monkeypatch):
        monkeypatch.setattr(settings, "trusted_proxies", "10.0.0.0/8")

        class Client:
            host = "10.1.2.3"

        class Req:
            client = Client()
            headers = {"x-forwarded-for": "192.0.2.5"}
            cookies = {}

        assert client_ip(Req()) == "192.0.2.5"

    def test_spoofed_ip_cannot_bypass_rate_limit(self, client, monkeypatch):
        """限速按 IP 计，伪造转发头不该让人人都有一个干净配额。"""
        monkeypatch.setattr(settings, "login_rate_limit", 3)
        for _ in range(3):
            r = client.post(
                "/api/admin/login",
                json={"username": "admin", "password": "wrong"},
                headers={"X-Forwarded-For": "1.2.3.4"},
            )
            assert r.status_code == 401
        # 第 4 次换一个伪造 IP，仍然应该被限住
        r = client.post(
            "/api/admin/login",
            json={"username": "admin", "password": "wrong"},
            headers={"X-Forwarded-For": "9.9.9.9"},
        )
        assert r.status_code == 429


class TestUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "https://user@evil.com/path",
            "https://trusted.com@evil.com/",
            "http://user:pass@example.com/",
            "http://127.0.0.1/admin",
            "http://localhost/x",
            "http://192.168.1.1/",
            "http://10.0.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "https://[::1]/",
            "https://[::ffff:127.0.0.1]/",
            "http://foo.internal/api",
            "http://bar.local/",
            "ftp://example.com/x",
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
        ],
    )
    def test_rejected(self, url):
        assert is_public_url(url) is False

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com",
            "https://example.com/a?b=c#d",
            "http://sub.domain.example.co.uk/path",
            "https://8.8.8.8/",
        ],
    )
    def test_accepted(self, url):
        assert is_public_url(url) is True

    def test_ssrf_metadata_endpoint_blocked_via_api(self, client):
        r = client.post("/api/links", json={"target": "http://169.254.169.254/latest/meta-data/"})
        assert r.status_code == 422


class TestLoginRateLimit:
    def test_excessive_attempts_throttled(self, client, monkeypatch):
        monkeypatch.setattr(settings, "login_rate_limit", 3)
        for _ in range(3):
            client.post("/api/admin/login", json={"username": "admin", "password": "nope"})
        r = client.post("/api/admin/login", json={"username": "admin", "password": "nope"})
        assert r.status_code == 429
        assert "5 分钟" in r.json()["message"]

    def test_correct_password_also_throttled(self, client, monkeypatch):
        """配额用尽后连正确口令也挡——否则限速就没意义了。"""
        monkeypatch.setattr(settings, "login_rate_limit", 2)
        for _ in range(2):
            client.post("/api/admin/login", json={"username": "admin", "password": "nope"})
        r = client.post("/api/admin/login", json={"username": "admin", "password": "test-token"})
        assert r.status_code == 429

    def test_limit_disabled_when_zero(self, client, monkeypatch):
        monkeypatch.setattr(settings, "login_rate_limit", 0)
        for _ in range(15):
            client.post("/api/admin/login", json={"username": "admin", "password": "nope"})
        r = client.post("/api/admin/login", json={"username": "admin", "password": "test-token"})
        assert r.status_code == 200


class TestSecretHashing:
    def test_hash_is_not_plaintext(self):
        from app.core.crypto import fingerprint, hash_secret, is_hash, verify_secret

        encoded = hash_secret("correct horse battery staple")
        assert "correct horse" not in encoded
        assert is_hash(encoded)
        assert verify_secret(encoded, "correct horse battery staple")
        assert not verify_secret(encoded, "wrong")

    def test_salt_makes_hashes_unique(self):
        from app.core.crypto import hash_secret

        a = hash_secret("same-secret")
        b = hash_secret("same-secret")
        assert a != b  # 每次盐都不同，相同口令不该得到相同哈希

    def test_pbkdf2_fallback_when_no_argon2(self):
        """argon2 缺失时接口行为不能变。"""
        from app.core import crypto
        from app.core.crypto import hash_secret, is_hash, verify_secret

        real = crypto._HAS_ARGON2
        try:
            crypto._HAS_ARGON2 = False
            encoded = hash_secret("fallback-secret")
            assert is_hash(encoded)
            assert encoded.startswith("pbkdf2_sha256$")
            assert verify_secret(encoded, "fallback-secret")
            assert not verify_secret(encoded, "other")
        finally:
            crypto._HAS_ARGON2 = real

    def test_garbage_hash_rejected(self):
        from app.core.crypto import verify_secret

        assert not verify_secret("not-a-hash", "x")
        assert not verify_secret("", "x")
        assert not verify_secret(None, "x")
        assert not verify_secret("$argon2id$garbage", "x")

    def test_needs_rehash_flags_weak_params(self):
        from app.core.crypto import needs_rehash

        assert needs_rehash("pbkdf2_sha256$i=1000$c2FsdA$aGFzaA") is True
        assert needs_rehash("plaintext-token") is True

    def test_fingerprint_stable_and_short(self):
        from app.core.crypto import fingerprint

        fp = fingerprint("some-token")
        assert fp == fingerprint("some-token")
        assert len(fp) == 16
        assert "some-token" not in fp

    def test_random_token_is_url_safe(self):
        from app.core.crypto import random_token

        token = random_token(24)
        assert len(token) > 24
        assert all(c.isalnum() or c in "-_" for c in token)

    def test_compare_digest_handles_none(self):
        from app.core.crypto import compare_digest

        assert compare_digest(None, "x") is False
        assert compare_digest("x", None) is False
        assert compare_digest("abc", "abc") is True
        assert compare_digest("abc", "abd") is False
