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

    def test_never_overwrites_itself_mid_run(self):
        """运行中不能把脚下的 install.sh 覆盖掉，否则后半截脚本就不是自己了。

        这条早先读作「源码清单里根本不许有 install.sh」。反了：脚本不更新，
        装得越早的机器越修不到 bug（新修法全在新脚本里）。正确的做法是
        「清单里有、但换的时机放在跑完之后」—— 由 apply_source 摘出来，
        到 self_update_script 才落盘。
        """
        text = self.script.read_text(encoding="utf-8")
        # 1) 源码清单里可以有它
        start = text.index("SOURCE_ENTRIES=")
        block = text[start : text.index('"', text.index("\n", start) + 1)]
        assert "install.sh" in block
        # 2) 但 apply_source 必须把它从待覆盖的文件里摘走
        apply = text[text.index("apply_source() {") :]
        apply = apply[: apply.index("\n}")]
        assert ".install.sh.pending" in apply, "apply_source 没有把 install.sh 摘出来"
        # 3) 落盘只发生在 self_update_script 里，而且判过语法 / shebang / 版本
        fn = text[text.index("self_update_script() {") :]
        fn = fn[: fn.index("\n}")]
        assert "bash -n" in fn and "is_shell_script" in fn and "version_lt" in fn
        # 4) 主流程最后才调它
        assert text.rindex("self_update_script") > text.index("install_cli")


class TestInstallScriptV2:
    """v2 安装脚本的硬规矩。

    这几条对应真实踩过的坑：
    - 管道里 `curl | bash` 也能跑，脚本不能依赖自身文件名是 install.sh
    - status 跑完必须 exit 0，否则 `simpleurl status && ...` 会莫名其妙断掉
    - 升级必须能回滚，且不许把「远端比本地旧」的代码覆盖回来
    - 反代配置要真的能挂上，不是一段装饰
    """

    script = ROOT / "install.sh"

    def text(self) -> str:
        return self.script.read_text(encoding="utf-8")

    def test_syntax_ok(self):
        r = subprocess.run(["bash", "-n", str(self.script)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_has_subcommands(self):
        body = self.text()
        for sub in ("install", "upgrade", "uninstall", "doctor", "help"):
            assert sub in body, f"缺子命令 {sub}"
        # 分支里得有真的动作，不是光列在帮助里
        assert "do_install" in body and "do_uninstall" in body

    def test_status_exits_zero(self):
        """status 是给人链式调用用的，最后一句不能是条件判断。"""
        body = self.text()
        assert "site_status" in body
        # 生成的管理命令里，status 的收尾必须显式 return 0
        assert "  return 0\n}" in body

    def test_upgrade_can_rollback(self):
        body = self.text()
        assert "rollback_or_die" in body or "do_rollback" in body
        assert ".backup" in body, "升级前必须备份数据"

    def test_refuses_downgrade(self):
        body = self.text()
        assert "version_lt" in body
        assert "比本地" in body

    def test_no_html_masquerading_as_tarball(self):
        """CNB 的 /-/raw 会返回 200 的 HTML 外壳，必须靠魔数挡掉。"""
        body = self.text()
        assert "is_tarball" in body
        assert "037\\213" in body or "\\037\\213" in body

    def test_private_repo_token_support(self):
        body = self.text()
        assert "CNB_TOKEN" in body

    def test_writes_compose_file(self):
        body = self.text()
        assert "write_compose_file" in body
        assert "env_file" in body

    def test_generates_proxy_config(self):
        body = self.text()
        for server in ("nginx", "caddy", "apache"):
            assert server in body, f"没考虑 {server}"
        assert "X-Forwarded-Proto" in body
        assert "certbot" in body

    def test_doctor_does_not_write(self):
        """doctor 只读：不该出现 mkdir/写入安装目录的调用在同一段里。"""
        body = self.text()
        start = body.index("doctor() {")
        end = body.index("# ---------------------------------------------------------------- 源码")
        segment = body[start:end]
        for forbidden in ("rm -rf", "mkdir -p \"$INSTALL_DIR\"", "install_python_mode"):
            assert forbidden not in segment, f"doctor 里不该出现 {forbidden}"

    def test_env_file_permissions(self):
        body = self.text()
        assert 'chmod 600 "$file"' in body or "chmod 600" in body


class TestInstallScriptContract:
    """脚本对外承诺的行为，用子进程真的跑一遍（不依赖网络）。

    这些都是「改一行就悄悄坏掉」的地方，纯文本断言挡不住：
    - `--help` 必须 0 退出，且列出全部子命令
    - `doctor` 必须只读：跑完安装目录里不该多出任何文件
    - 未知参数必须非 0，不能默默当默认安装跑
    """

    script = ROOT / "install.sh"

    def run(self, *args, env=None):
        import os

        e = dict(os.environ)
        e.pop("CNB_TOKEN", None)          # 别把工作区的令牌带进测试
        e.update(env or {})
        return subprocess.run(
            ["bash", str(self.script), *args],
            capture_output=True, text=True, env=e, timeout=120,
        )

    def test_help_exits_zero_and_lists_commands(self):
        r = self.run("--help")
        assert r.returncode == 0, r.stderr
        for word in ("doctor", "upgrade", "uninstall", "--yes"):
            assert word in r.stdout, f"帮助里没提 {word}"

    def test_unknown_flag_fails(self):
        r = self.run("--nope-nope")
        assert r.returncode != 0
        assert "未知参数" in (r.stdout + r.stderr)

    def test_doctor_writes_nothing(self, tmp_path):
        home = tmp_path / "install"
        r = self.run("doctor", env={"SIMPLEURL_HOME": str(home), "PORT": "59999"})
        # 体检结果允许失败（缺依赖等），但目录必须原样不动
        assert not home.exists(), "doctor 不该创建安装目录"
        assert "环境体检" in r.stdout

    def test_upgrade_without_install_refuses(self, tmp_path):
        home = tmp_path / "nope"
        home.mkdir()
        r = self.run("upgrade", env={"SIMPLEURL_HOME": str(home)})
        assert r.returncode != 0
        assert "没有安装记录" in (r.stdout + r.stderr)

    def test_uninstall_refuses_foreign_dir(self, tmp_path):
        home = tmp_path / "someone-elses"
        home.mkdir()
        (home / "keep.txt").write_text("别删我")
        r = self.run("--uninstall", "--force", env={"SIMPLEURL_HOME": str(home)})
        assert r.returncode != 0
        assert (home / "keep.txt").exists(), "不该删别人的目录"

    def test_tarball_magic_check(self, tmp_path):
        """HTML 外壳不能被当成 tar.gz 解开。"""
        fake = tmp_path / "fake.tar.gz"
        fake.write_text("<!DOCTYPE html><html>Not Found</html>")
        code = (
            'source <(sed -n "/^is_tarball()/,/^}/p" "$0"); '
            f'is_tarball "{fake}" && echo yes || echo no'
        )
        r = subprocess.run(["bash", "-c", code, str(self.script)], capture_output=True, text=True)
        assert r.stdout.strip() == "no"

    def test_version_compare(self):
        code = (
            'source <(sed -n "/^version_lt()/,/^}/p" "$0"); '
            'version_lt 1.5.0 2.0.0 && echo lt; '
            'version_lt 2.0.0 2.0.0 && echo eq || echo ne'
        )
        r = subprocess.run(["bash", "-c", code, str(self.script)], capture_output=True, text=True)
        assert "lt" in r.stdout and "ne" in r.stdout
