"""安全加固：响应头、CSP、密钥哈希、会话绑定、IP 信任。"""

from __future__ import annotations

import pytest

from app.core import auth, crypto
from app.core.config import settings
from app.core.middleware import build_csp
from app.core.security import client_ip


def login(client, secret: str = "root-secret"):
    return client.post("/api/admin/login", headers={"X-Admin-Token": secret})


def new_link(client, **payload):
    """创建链接，非 201 直接抛，避免后面拿到错误响应再炸一遍。"""
    payload.setdefault("target", "https://example.com/page")
    r = client.post("/api/links", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def try_link(client, **payload):
    """创建链接并原样返回响应，用于断言被拒绝的场景。"""
    payload.setdefault("target", "https://example.com/page")
    return client.post("/api/links", json=payload)


@pytest.fixture()
def secret_key(monkeypatch):
    """固定会话签名密钥，避免测试间随机密钥让 Cookie 失效。"""
    monkeypatch.setattr(settings, "secret_key", "test-secret-key")
    return "test-secret-key"


class FakeClient:
    def __init__(self, host="1.2.3.4", headers=None):
        self.host = host
        self.headers = headers or {}


class FakeRequest:
    def __init__(self, host="1.2.3.4", headers=None, cookies=None):
        self.client = FakeClient(host) if host else None
        self.headers = headers or {}
        self.cookies = cookies or {}


# ---------------- 响应头 ----------------
class TestSecurityHeaders:
    def test_page_has_full_headers(self, client, secret_key):
        r = client.get("/")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert "geolocation=()" in r.headers["permissions-policy"]
        assert r.headers["cross-origin-opener-policy"] == "same-origin"
        assert r.headers["cross-origin-resource-policy"] == "same-origin"
        assert r.headers["x-request-id"]

    def test_no_hsts_over_plain_http(self, client, secret_key):
        # 本地 http 不该发 HSTS，否则浏览器会把 localhost 锁成 https
        assert "strict-transport-security" not in client.get("/").headers

    def test_hsts_over_https(self, client, secret_key):
        r = client.get("/", headers={"x-forwarded-proto": "https"})
        assert "max-age=63072000" in r.headers["strict-transport-security"]

    def test_api_also_gets_headers(self, client, secret_key):
        r = client.get("/api/access")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-request-id"]

    def test_static_gets_headers(self, client, secret_key):
        r = client.get("/static/app.css")
        assert r.status_code == 200
        assert r.headers["x-content-type-options"] == "nosniff"

    def test_request_id_is_echoed(self, client, secret_key):
        r = client.get("/", headers={"x-request-id": "trace-123"})
        assert r.headers["x-request-id"] == "trace-123"


# ---------------- CSP ----------------
class TestCSP:
    def test_document_csp_has_nonce(self, client, secret_key):
        csp = client.get("/").headers["content-security-policy"]
        assert "script-src 'self' 'nonce-" in csp
        assert "object-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        # 不该出现会放开任意源的通配
        assert "*" not in csp.replace("'nonce-", "").split(";")[1]

    def test_nonce_changes_per_request(self, client, secret_key):
        first = client.get("/").headers["content-security-policy"]
        second = client.get("/").headers["content-security-policy"]
        assert first != second

    def test_nonce_matches_script_tag(self, client, secret_key):
        r = client.get("/")
        csp = r.headers["content-security-policy"]
        nonce = csp.split("'nonce-")[1].split("'")[0]
        assert f'nonce="{nonce}"' in r.text

    def test_resource_csp_is_locked_down(self, client, secret_key):
        csp = client.get("/api/access").headers["content-security-policy"]
        assert "sandbox" in csp
        assert "nonce" not in csp

    def test_report_only_mode(self, client, secret_key, monkeypatch):
        monkeypatch.setattr(settings, "csp_report_only", True)
        r = client.get("/")
        assert "content-security-policy" not in r.headers
        assert "content-security-policy-report-only" in r.headers

    def test_build_csp_without_nonce(self):
        csp = build_csp(None)
        assert csp == "frame-ancestors 'none'; base-uri 'none'; sandbox"

    def test_no_unsafe_inline_script(self, client, secret_key):
        # script-src 绝不允许 unsafe-inline，否则 nonce 就白做了
        csp = client.get("/").headers["content-security-policy"]
        script_src = [d for d in csp.split(";") if "script-src" in d][0]
        assert "unsafe-inline" not in script_src


# ---------------- 密钥哈希 ----------------
class TestSecretHashing:
    def test_argon2_roundtrip(self):
        encoded = crypto.hash_secret("root-secret")
        assert encoded.startswith("$argon2id$")
        assert crypto.verify_secret("root-secret", encoded)
        assert not crypto.verify_secret("wrong", encoded)

    def test_hash_is_salted(self):
        a, b = crypto.hash_secret("same"), crypto.hash_secret("same")
        assert a != b  # 每次盐都不同
        assert crypto.verify_secret("same", a) and crypto.verify_secret("same", b)

    def test_hash_hides_plaintext(self):
        encoded = crypto.hash_secret("root-secret")
        assert "root-secret" not in encoded

    def test_rejects_empty_and_none(self):
        encoded = crypto.hash_secret("root-secret")
        assert not crypto.verify_secret("", encoded)
        assert not crypto.verify_secret(None, encoded)
        assert crypto.hash_secret("") == ""

    def test_rejects_garbage(self):
        assert not crypto.verify_secret("x", "not-a-hash")
        assert not crypto.verify_secret("x", "argon2id$broken")
        assert not crypto.verify_secret("x", "$argon2id$v=19$m=1,t=1,p=1$aa$bb")

    def test_admin_token_accepts_hash_config(self, client, mode, secret_key):
        """ADMIN_TOKEN 可以直接填 Argon2id 哈希，配置文件泄露也拿不回密钥。"""
        encoded = crypto.hash_secret("root-secret")
        mode("admin", token=encoded)
        assert login(client).status_code == 200
        assert client.get("/api/links").status_code == 200

    def test_hash_config_rejects_wrong_secret(self, client, mode, secret_key):
        mode("admin", token=crypto.hash_secret("root-secret"))
        assert login(client, "nope").status_code == 401
        assert client.get("/api/links").status_code == 401

    def test_needs_rehash_flags_weak_params(self):
        strong = crypto.hash_secret("s")
        assert not crypto.needs_rehash(strong)
        assert crypto.needs_rehash("pbkdf2_sha256$i=1000$aa$bb")
        assert crypto.needs_rehash(None)

    def test_pbkdf2_fallback_still_verifies(self, monkeypatch):
        """缺 argon2 的环境里，PBKDF2 编码必须仍能校验。"""
        monkeypatch.setattr(crypto, "_HAS_ARGON2", False)
        encoded = crypto.hash_secret("secret")
        assert encoded.startswith("pbkdf2_sha256$")
        assert crypto.verify_secret("secret", encoded)
        assert not crypto.verify_secret("nope", encoded)

    def test_compare_digest_handles_none(self):
        assert not crypto.compare_digest(None, "a")
        assert not crypto.compare_digest("a", None)
        assert crypto.compare_digest("a", "a")


# ---------------- 会话绑定 ----------------
class TestSessionBinding:
    def test_cookie_dies_when_admin_token_rotates(self, client, mode, secret_key):
        """轮换 ADMIN_TOKEN 后旧会话必须立刻失效。"""
        mode("admin", token="old-secret")
        assert login(client, "old-secret").status_code == 200
        assert client.get("/api/links").status_code == 200
        # 运维换密钥
        settings.admin_token = "new-secret"
        assert client.get("/api/links").status_code == 401

    def test_cookie_dies_when_admin_token_cleared(self, client, mode, secret_key):
        """停用管理密钥后，旧会话必须失效。

        注意此时站点会自动退回开放模式（这是防锁死的既定设计），
        所以这里验的是「会话本身不再被认作管理员」，而不是接口返回 401。
        """
        mode("admin", token="root-secret")
        assert login(client, "root-secret").status_code == 200
        assert client.get("/api/links").status_code == 200
        mode("admin", token="")
        data = client.get("/api/access").json()
        # 无密钥时自动退回开放模式，但旧 Cookie 已不被认作管理员
        assert data["is_admin"] is False
        assert data["mode"] == "public"

    def test_cookie_dies_when_secret_key_rotates(self, client, mode, secret_key):
        """轮换 SECRET_KEY 等于全站重新登录。"""
        mode("admin", token="root-secret")
        login(client)
        settings.secret_key = "another-secret"
        assert client.get("/api/links").status_code == 401

    def test_session_not_derived_from_admin_token(self, mode, secret_key):
        """会话签名密钥与管理密钥无关：知道密钥也伪造不出 Cookie。"""
        mode("admin", token="root-secret")
        token = auth.admin_session()
        assert "root-secret" not in token
        assert auth.is_admin_token(token)
        settings.admin_token = "other"
        assert not auth.is_admin_token(token)

    def test_expired_token_rejected(self, mode, secret_key):
        mode("admin", token="root-secret")
        token = auth.admin_session(ttl=-10)
        assert not auth.is_admin_token(token)

    def test_forged_cookie_rejected(self, client, mode, secret_key):
        mode("admin")
        client.cookies.set(auth.ADMIN_COOKIE, "1700000000.deadbeef")
        assert client.get("/api/links").status_code == 401

    def test_empty_cookie_rejected(self, client, mode, secret_key):
        mode("admin")
        client.cookies.set(auth.ADMIN_COOKIE, "")
        assert client.get("/api/links").status_code == 401

    def test_invite_cookie_dies_with_token(self, client, mode, secret_key):
        mode("invite", token="root-secret")
        assert client.post(f"/api/invite?code={auth.invite_key()}").status_code == 200
        new_link(client, code="inv-ok")
        # 轮换管理密钥后，邀请码本身也变了，旧解锁 Cookie 随之作废
        mode("invite", token="rotated")
        assert client.get("/api/access").json()["can_create"] is False
        assert client.post(f"/api/invite?code={auth.invite_key()}").status_code == 200
        new_link(client, code="inv-new")


# ---------------- Cookie 属性 ----------------
class TestCookieFlags:
    def test_cookie_is_httponly_samesite(self, client, mode, secret_key):
        mode("admin", token="root-secret")
        r = login(client, "root-secret")
        cookie = r.headers.get("set-cookie", "")
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert "Path=/" in cookie

    def test_cookie_secure_over_https(self, client, mode, secret_key):
        mode("admin", token="root-secret")
        r = client.post(
            "/api/admin/login",
            headers={"X-Admin-Token": "root-secret", "x-forwarded-proto": "https"},
        )
        assert "Secure" in r.headers.get("set-cookie", "")

    def test_cookie_not_secure_over_http(self, client, mode, secret_key):
        # 本地 http 开发不能直接变砖
        mode("admin", token="root-secret")
        r = login(client, "root-secret")
        assert "Secure" not in r.headers.get("set-cookie", "")


# ---------------- 登录接口收紧 ----------------
class TestLoginHardening:
    def test_query_string_token_rejected(self, client, mode, secret_key):
        """密钥走 URL 会进日志和 Referer，这里明确拒绝。"""
        mode("admin", token="root-secret")
        r = client.post("/api/admin/login?token=root-secret")
        assert r.status_code == 400
        assert "X-Admin-Token" in r.json()["message"]

    def test_missing_secret_rejected(self, client, mode, secret_key):
        mode("admin", token="root-secret")
        assert client.post("/api/admin/login").status_code == 400

    def test_qr_requires_header_not_query(self, client, mode, secret_key):
        mode("admin", token="root-secret")
        assert login(client, "root-secret").status_code == 200
        new_link(client, code="qr-sec")
        client.post("/api/admin/logout")
        # query 传密钥已废止，一律按未登录处理
        assert client.get("/api/qr/qr-sec?token=root-secret").status_code == 401
        assert client.get("/api/qr/qr-sec", headers={"X-Admin-Token": "root-secret"}).status_code == 200

    def test_login_is_rate_limited(self, client, mode, secret_key):
        mode("admin", token="root-secret")
        codes = [login(client, "wrong").status_code for _ in range(12)]
        assert 429 in codes
        # 限速期间即使密钥正确也进不来
        assert login(client, "root-secret").status_code == 429

    def test_logout_clears_both_cookies(self, client, mode, secret_key):
        mode("invite", token="root-secret")
        login(client, "root-secret")
        client.post(f"/api/invite?code={auth.invite_key()}")
        r = client.post("/api/admin/logout")
        assert r.status_code == 200
        assert client.get("/api/links").status_code == 401


# ---------------- IP 信任 ----------------
class TestClientIP:
    def test_forwarded_header_ignored_by_default(self, monkeypatch):
        """默认不信反代头，否则限速能被伪造 IP 绕过。"""
        monkeypatch.setattr(settings, "trusted_proxies", [])
        req = FakeRequest(headers={"x-forwarded-for": "9.9.9.9"})
        assert client_ip(req) == "1.2.3.4"

    def test_forwarded_header_trusted_when_proxy_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "trusted_proxies", ["10.0.0.1"])
        req = FakeRequest(host="10.0.0.1", headers={"x-forwarded-for": "9.9.9.9, 10.0.0.1"})
        assert client_ip(req) == "9.9.9.9"

    def test_takes_rightmost_untrusted_hop(self, monkeypatch):
        """多级反代时取最右侧的非信任地址，那才是反代真实看到的客户端。"""
        monkeypatch.setattr(settings, "trusted_proxies", ["10.0.0.1", "10.0.0.2"])
        req = FakeRequest(host="10.0.0.2", headers={"x-forwarded-for": "1.1.1.1, 10.0.0.1"})
        assert client_ip(req) == "1.1.1.1"

    def test_spoofed_forwarded_without_proxy_config_ignored(self, monkeypatch):
        monkeypatch.setattr(settings, "trusted_proxies", [])
        req = FakeRequest(headers={"x-real-ip": "9.9.9.9"})
        assert client_ip(req) == "1.2.3.4"

    def test_no_client_returns_none(self, monkeypatch):
        monkeypatch.setattr(settings, "trusted_proxies", [])
        assert client_ip(FakeRequest(host=None)) is None

    def test_rate_limit_uses_direct_ip(self, monkeypatch):
        """伪造 XFF 不该刷新限速计数。"""
        from app.core.security import rate_limiter

        monkeypatch.setattr(settings, "trusted_proxies", [])
        rate_limiter.reset()
        req = FakeRequest(headers={"x-forwarded-for": "9.9.9.9"})
        assert rate_limiter.allow(client_ip(req), 1, 60) is True
        assert rate_limiter.allow(client_ip(req), 1, 60) is False


# ---------------- 跳转安全 ----------------
class TestRedirectSafety:
    def test_open_redirect_blocked(self, client, mode, secret_key):
        """短码目标必须被补成完整 URL，不能留成协议相对地址。"""
        mode("public")
        # 协议相对地址会被补成绝对 https，落地明确（纯数字短码会被当成端口号，这里不用）
        assert new_link(client, target="//evil.com/x", code="slashes")["target"] == "https://evil.com/x"
        assert new_link(client, target="evil.com/x", code="noproto")["target"] == "https://evil.com/x"
        # 但危险协议一律拒掉
        for bad in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "ftp://x.com/f"):
            r = try_link(client, target=bad)
            assert r.status_code == 422, (bad, r.text)

    def test_redirect_uses_307(self, client, mode, secret_key):
        mode("public")
        new_link(client, code="redir1")
        r = client.get("/redir1", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "https://example.com/page"

    def test_admin_mode_qr_hidden_from_guest(self, client, mode, secret_key):
        """私有模式下二维码等于泄露目标地址，访客拿不到。"""
        mode("admin", token="root-secret")
        assert login(client, "root-secret").status_code == 200
        new_link(client, code="qr-priv")
        client.post("/api/admin/logout")
        assert client.get("/api/qr/qr-priv").status_code == 401

    def test_stats_page_hidden_from_guest(self, client, mode, secret_key):
        mode("admin", token="root-secret")
        assert login(client, "root-secret").status_code == 200
        new_link(client, code="st-priv")
        client.post("/api/admin/logout")
        assert client.get("/s/st-priv").status_code == 401


@pytest.fixture()
def mode(monkeypatch):
    """切换访问模式，用例结束后还原。"""

    def _set(value: str, token: str = "root-secret"):
        monkeypatch.setattr(settings, "access_mode", value)
        monkeypatch.setattr(settings, "admin_token", token)
        auth._login_hits.clear()

    return _set
