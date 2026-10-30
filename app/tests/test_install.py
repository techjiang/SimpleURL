"""安装链路相关的行为：账号密码形状、对外地址、管理脚本的可用性。

这几条都是「装完才发现不对」的那类问题，值得钉住：
- 安装脚本写的是 `ADMIN_TOKEN=账号:密码`，后台得认得出这是账号密码
- 配了 PUBLIC_URL，短链和二维码就得用对外域名，不能拼出 127.0.0.1
- install.sh 得有可执行位，且语法能被 shell 解析
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.settings_store import effective

ROOT = Path(__file__).resolve().parents[2]


class TestAdminTokenShape:
    """ADMIN_TOKEN 的两种写法都要认。"""

    @pytest.mark.parametrize(
        ("token", "user", "pwd"),
        [
            ("admin:abc12345", "admin", "abc12345"),
            ("boss:p@ss:with:colons", "boss", "p@ss:with:colons"),
            ("just-a-plain-token", "admin", "just-a-plain-token"),
        ],
    )
    def test_parse(self, monkeypatch, token, user, pwd):
        from app.core import admin

        monkeypatch.setattr(settings, "admin_token", token)
        assert admin.check_credentials(user, pwd) is True

    def test_wrong_password_rejected(self, monkeypatch):
        from app.core import admin

        monkeypatch.setattr(settings, "admin_token", "admin:abc12345")
        assert admin.check_credentials("admin", "abc12346") is False
        assert admin.check_credentials("root", "abc12345") is False

    def test_empty_token_disables_login(self, monkeypatch):
        from app.core import admin

        monkeypatch.setattr(settings, "admin_token", "")
        assert admin.is_admin_enabled() is False
        assert admin.check_credentials("admin", "") is False


def new_link(client, code: str, target: str = "https://example.com/a-long-url") -> dict:
    """建一条短链。code 固定，便于断言对外地址的前缀。"""
    r = client.post("/api/links", json={"target": target, "code": code})
    assert r.status_code == 201, r.text
    return r.json()


class TestPublicUrl:
    """对外地址：后台设的优先，其次环境变量，最后才用请求里的 Host。"""

    def test_env_public_url_wins(self, client, monkeypatch):
        monkeypatch.setattr(settings, "public_url", "https://s.example.com")
        data = new_link(client, code="envurl")
        assert data["short_url"] == "https://s.example.com/envurl"

    def test_settings_override_env(self, client, monkeypatch):
        from app.core.db import session_scope
        from app.core.settings_store import set_value
        from app.core import policy

        monkeypatch.setattr(settings, "public_url", "https://env.example.com")
        with session_scope() as db:
            set_value(db, "public_url", "https://db.example.com")
        policy.invalidate()
        try:
            data = new_link(client, code="dburl")
            assert data["short_url"] == "https://db.example.com/dburl"
        finally:
            with session_scope() as db:
                set_value(db, "public_url", "")
            policy.invalidate()

    def test_falls_back_to_request_host(self, client, monkeypatch):
        monkeypatch.setattr(settings, "public_url", "")
        data = new_link(client, code="hosturl")
        assert data["short_url"].endswith("/hosturl")
        assert "127.0.0.1" in data["short_url"] or "testserver" in data["short_url"]

    def test_public_url_is_editable(self):
        from app.core.settings_store import EDITABLE

        assert "public_url" in EDITABLE


class TestInstallScript:
    """脚本本身的底线：能解析、有可执行位、不写死认不出的凭据。"""

    script = ROOT / "install.sh"

    def test_exists_and_executable(self):
        assert self.script.exists()
        assert self.script.stat().st_mode & 0o111, "install.sh 丢了可执行位"

    def test_syntax_ok(self):
        r = subprocess.run(["bash", "-n", str(self.script)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_writes_admin_token_pair(self):
        """装完生成的是 ADMIN_TOKEN=账号:密码，不是老的两行分开写。"""
        text = self.script.read_text(encoding="utf-8")
        assert "ADMIN_TOKEN=$ADMIN_USER:$ADMIN_PASS" in text
        assert "ADMIN_PASSWORD=" not in text

    def test_has_management_commands(self):
        text = self.script.read_text(encoding="utf-8")
        for sub in ("status", "log", "password", "backup", "restart", "url", "config"):
            assert f"  {sub})" in text or f"{sub}|" in text, f"缺管理子命令 {sub}"

    def test_refuses_unsafe_uninstall(self):
        """不是本脚本装的目录不许删——误删数据比装不上严重得多。"""
        text = self.script.read_text(encoding="utf-8")
        assert "MARK_FILE" in text
        assert "不是本脚本装的目录" in text

    def test_never_overwrites_itself(self):
        """更新代码时不能把 install.sh 也覆盖掉，否则下次跑到旧脚本。"""
        text = self.script.read_text(encoding="utf-8")
        start = text.index("SOURCE_ENTRIES=")
        block = text[start : text.index('"', text.index("\n", start) + 1)]
        assert "install.sh" not in block
