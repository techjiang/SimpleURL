"""安装脚本 v2.6 与服务端「账号密码」凭据。

这轮的主线是同一件事：一键装完，屏幕上给的那串密码必须**真能登进后台**。
之前安装脚本写 ADMIN_TOKEN=账号:密码、服务端靠拆字符串凑账号密码，
两边各持一半约定；现在服务端认 ADMIN_USERNAME / ADMIN_PASSWORD，
老写法仍然认。下面把这些边界钉住。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from app.core import admin as admin_core
from app.core.config import settings

SCRIPT = Path(__file__).resolve().parents[2] / "install.sh"


@pytest.fixture()
def creds(monkeypatch):
    """一份干净的账号密码配置：不掺 ADMIN_TOKEN。"""
    monkeypatch.setattr(settings, "admin_username", "admin")
    monkeypatch.setattr(settings, "admin_password", "correct-horse-9")
    monkeypatch.setattr(settings, "admin_token", "")
    return settings


class TestCredentials:
    def test_username_password(self, creds):
        assert admin_core.check_credentials("admin", "correct-horse-9")

    def test_username_is_case_insensitive(self, creds):
        """安装时抄成 Admin 不算错——把人自己关在门外才是错。"""
        assert admin_core.check_credentials("Admin", "correct-horse-9")

    def test_wrong_password(self, creds):
        assert not admin_core.check_credentials("admin", "nope")
        assert not admin_core.check_credentials("root", "correct-horse-9")
        assert not admin_core.check_credentials("", "correct-horse-9")

    def test_legacy_token_pair_still_works(self, monkeypatch):
        """老部署只写了 ADMIN_TOKEN=admin:password，升级后必须照样进得去。"""
        monkeypatch.setattr(settings, "admin_username", "admin")
        monkeypatch.setattr(settings, "admin_password", "")
        monkeypatch.setattr(settings, "admin_token", "boss:legacy-pass")
        assert admin_core.check_credentials("boss", "legacy-pass")
        assert not admin_core.check_credentials("boss", "admin")

    def test_bare_token_means_admin_account(self, monkeypatch):
        monkeypatch.setattr(settings, "admin_username", "admin")
        monkeypatch.setattr(settings, "admin_password", "")
        monkeypatch.setattr(settings, "admin_token", "just-a-passphrase")
        assert admin_core.check_credentials("admin", "just-a-passphrase")

    def test_disabled_when_nothing_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "admin_username", "admin")
        monkeypatch.setattr(settings, "admin_password", "")
        monkeypatch.setattr(settings, "admin_token", "")
        assert admin_core.is_admin_enabled() is False
        assert admin_core.check_credentials("admin", "") is False


class TestWeakPassword:
    def test_short_password_password_source_locks_guests(self, monkeypatch, anon_client):
        """口令闸不能只看 ADMIN_TOKEN：靠 ADMIN_PASSWORD 装的站也要拦。"""
        monkeypatch.setattr(settings, "admin_token", "")
        monkeypatch.setattr(settings, "admin_password", "123456")
        data = anon_client.get("/api/access").json()
        assert data["locked"] is True
        assert any("ADMIN_PASSWORD" in w["text"] for w in data["warnings"])

    def test_strong_password_not_locked(self, monkeypatch, anon_client):
        monkeypatch.setattr(settings, "admin_token", "")
        monkeypatch.setattr(settings, "admin_password", "a-very-long-passphrase")
        assert anon_client.get("/api/access").json()["locked"] is False


class TestLoginPage:
    def test_login_with_password_only_config(self, monkeypatch, anon_client):
        """服务端只配了 ADMIN_PASSWORD 时，登录接口照样能进。"""
        monkeypatch.setattr(settings, "admin_token", "")
        monkeypatch.setattr(settings, "admin_username", "admin")
        monkeypatch.setattr(settings, "admin_password", "a-very-long-passphrase")
        r = anon_client.post(
            "/api/admin/login", json={"username": "admin", "password": "a-very-long-passphrase"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

    def test_login_rejects_bad_password(self, monkeypatch, anon_client):
        monkeypatch.setattr(settings, "admin_token", "")
        monkeypatch.setattr(settings, "admin_username", "admin")
        monkeypatch.setattr(settings, "admin_password", "a-very-long-passphrase")
        r = anon_client.post("/api/admin/login", json={"username": "admin", "password": "wrong-one"})
        assert r.status_code == 401


class TestCredentialsEndpoint:
    def test_requires_current_password(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv("SIMPLEURL_ENV_FILE", str(tmp_path / ".env"))
        r = client.post("/api/admin/credentials", json={"password": "new-password-1", "current_password": "nope"})
        assert r.status_code == 403

    def test_rejects_short_password(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv("SIMPLEURL_ENV_FILE", str(tmp_path / ".env"))
        r = client.post(
            "/api/admin/credentials",
            json={"password": "short", "current_password": "test-token-please-rotate"},
        )
        assert r.status_code == 400

    def test_updates_and_writes_back(self, client, tmp_path, monkeypatch):
        """改完立刻生效，而且写进 .env——否则重启就退回去了。"""
        env = tmp_path / ".env"
        env.write_text("ADMIN_TOKEN=admin:test-token-please-rotate\nSITE_NAME=x\n", encoding="utf-8")
        monkeypatch.setenv("SIMPLEURL_ENV_FILE", str(env))
        old_user, old_pass, old_token = settings.admin_username, settings.admin_password, settings.admin_token
        try:
            r = client.post(
                "/api/admin/credentials",
                json={
                    "username": "owner",
                    "password": "brand-new-pass",
                    "current_password": "test-token-please-rotate",
                },
            )
            assert r.status_code == 200, r.text
            assert r.json()["persisted"] is True
            assert admin_core.check_credentials("owner", "brand-new-pass")
            text = env.read_text(encoding="utf-8")
            assert "ADMIN_USERNAME=owner" in text
            assert "ADMIN_PASSWORD=brand-new-pass" in text
            # 旧的独立令牌一并作废：留着就是多一把没人记得的钥匙
            assert "ADMIN_TOKEN=admin:test-token-please-rotate" not in text
            # 别人的行不许动
            assert "SITE_NAME=x" in text
        finally:
            settings.admin_username, settings.admin_password, settings.admin_token = old_user, old_pass, old_token

    def test_unwritable_env_still_takes_effect(self, client, tmp_path, monkeypatch):
        """容器只读挂载时，改动照旧生效，只是要老实说「重启后会退回去」。"""
        monkeypatch.setenv("SIMPLEURL_ENV_FILE", str(tmp_path / "no-such-dir" / ".env"))
        old_user, old_pass, old_token = settings.admin_username, settings.admin_password, settings.admin_token
        try:
            r = client.post(
                "/api/admin/credentials",
                json={"password": "another-good-pass", "current_password": "test-token-please-rotate"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["persisted"] is False
            assert "重启后" in r.json()["message"]
        finally:
            settings.admin_username, settings.admin_password, settings.admin_token = old_user, old_pass, old_token


class TestInstallScript:
    @pytest.fixture(scope="class")
    def text(self):
        return SCRIPT.read_text(encoding="utf-8")

    def test_writes_credentials_not_token_pair(self, text):
        assert "ADMIN_USERNAME=$ADMIN_USER" in text
        assert "ADMIN_PASSWORD=$ADMIN_PASS" in text
        assert "ADMIN_TOKEN=$ADMIN_USER:$ADMIN_PASS" not in text

    def test_docker_gets_credentials(self, text):
        """Docker 那条路同样得把账号密码交给容器，否则两种装法行为不一。"""
        assert '-e "ADMIN_USERNAME=$ADMIN_USER" -e "ADMIN_PASSWORD=$ADMIN_PASS"' in text

    def test_reads_back_old_installs(self, text):
        """老安装的 .env 里只有 ADMIN_TOKEN：升级时得读得回来，不能重置密码。"""
        body = text[text.index("load_existing_env()"):]
        body = body[: body.index("\n}\n")]
        assert "ADMIN_PASSWORD" in body
        assert "ADMIN_TOKEN" in body

    def test_password_command_uses_env_put(self, text):
        """`simpleurl password` 要写账号密码两行，并且顺手把旧令牌清掉。"""
        body = text[text.index("set_password() {"):]
        body = body[: body.index("\n}\n")]
        assert 'env_put "$DIR/.env" ADMIN_PASSWORD' in body
        assert 'env_put "$DIR/.env" ADMIN_TOKEN ""' in body

    def test_env_put_is_portable(self, text):
        """sed -i 的 BSD / GNU 参数不同，之前为此写过两套分支；这里用 awk 一套了事。"""
        body = text[text.index("env_put() {"):]
        body = body[: body.index("\n}\n")]
        assert "awk" in body
        assert "sed -i" not in body
        assert re.search(r"index\(\$0, k \"=\"\) == 1", body)


class TestDockerEnvExample:
    def test_example_documents_credentials(self):
        example = (SCRIPT.parent / ".env.example").read_text(encoding="utf-8")
        assert "ADMIN_USERNAME=admin" in example
        assert "ADMIN_PASSWORD=" in example


class TestUninstallScope:
    """卸载只动自己那份：两份安装共用一个命令名，删错就把隔壁断了。"""

    @pytest.fixture(scope="class")
    def text(self):
        return SCRIPT.read_text(encoding="utf-8")

    def _body(self, text):
        return text[text.index("do_uninstall() {"):].split("\n}\n", 1)[0]

    def test_removes_only_commands_pointing_at_this_dir(self, text):
        body = self._body(text)
        assert 'if [ "$cdir" = "$INSTALL_DIR" ]' in body
        # 老写法无条件删两个路径，等于把隔壁那份也一起干掉
        assert 'rm -f /usr/local/bin/simpleurl "$HOME/.local/bin/simpleurl"' not in body

    def test_hands_command_back_to_remaining_install(self, text):
        """本目录删了，隔壁还在：全局命令要接回去，别让人以为装的东西没了。"""
        body = self._body(text)
        assert "still_there" in body
        assert "cp -f" in body and "/usr/local/bin/simpleurl" in body

    def test_never_regenerates_a_command_for_itself(self, text):
        """still_there 只能是别的目录，否则会把自己那份命令重新造出来。"""
        body = self._body(text)
        assert '[ "$still_there" != "$INSTALL_DIR" ]' in body

    def test_uninstall_loop_scans_both_paths(self, text):
        """`[ -f ] || continue` 会让 bash 把整段 for 当函数体跳过，第二个路径永不检查。"""
        body = self._body(text)
        loop = body[body.index("for f in /usr/local/bin/simpleurl"):]
        loop = loop[: loop.index("\n  done")]
        assert "|| continue" not in loop
        assert loop.count("for f in") == 1


def _inside_func(lines, idx):
    """粗略判断第 idx 行是不是在某个函数体内：往前找最近的顶层 `name() {`。

    只需要「够用」——把这个测试的误报压住，真正的判据是下面的 bash 实跑。
    """
    for j in range(idx, -1, -1):
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{", lines[j]) or lines[j].rstrip() == "main() {":
            return True
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{", lines[j]):
            return True
    return False


class TestCliForwarding:
    @pytest.fixture(scope="class")
    def text(self):
        return SCRIPT.read_text(encoding="utf-8")

    def test_install_script_accepts_dash_uninstall(self, text):
        """生成的命令会 `install.sh uninstall`；带杠的写法也认，免得多一个坑。"""
        assert "uninstall|remove)" in text

    def test_cli_passes_force_when_reinstalling(self, text):
        """拷到临时目录那份再问一遍，非交互调用就永远卡住。"""
        assert 'exec bash "$tmp/install.sh" uninstall --force' in text

    def test_cli_falls_back_to_install_script(self, text):
        """目录里的影子命令被删了，装着的 install.sh 还在，别报「找不到命令」。"""
        assert '[ -x "$INSTALL_DIR/install.sh" ]' in text

    def test_cli_case_arms_have_no_local(self, tmp_path):
        """顶部 case 不是函数体：写 local 会当场 `local: can only be used in a function`。

        不靠缩进猜哪段是 case —— 直接看行首，再拿 bash 实跑一遍那几个分支。
        """
        text = SCRIPT.read_text(encoding="utf-8")
        body = text[text.index("tee \"$rendered\" >/dev/null <<'SU_CLI_EOF'"):]
        body = body[:body.index("\nSU_CLI_EOF\n")]
        lines = body.splitlines()
        offenders = []
        for i, line in enumerate(lines):
            if not line.startswith((" ", "\t")) or re.match(r"^\s*(if|elif|else|case|for|while|\}|fi|esac|done)\b", line):
                continue
            if re.match(r"^\s{4,}local\s", line):
                # 只有「行首即缩进」的那几段（case 分支体）才可能踩这个坑：
                # 上游缩进是 4 格，函数体里至少再往里一层
                depth = len(line) - len(line.lstrip())
                if depth <= 6 and not _inside_func(lines, i):
                    offenders.append(line.strip())
        assert not offenders, f"case 分支里出现 local：{offenders}"

        # 真跑一遍：这两个分支以前就是在这里炸的
        cli = tmp_path / "simpleurl"
        cli.write_text(
            body.replace("__SU_VERSION__", "0.0.0").replace("__SU_NAME__", "simpleurl")
            .replace("__SU_DIR__", str(tmp_path)),
            encoding="utf-8",
        )
        (tmp_path / ".simpleurl-managed").write_text("runtime=python\nport=1\n", encoding="utf-8")
        (tmp_path / ".env").write_text("PORT=1\n", encoding="utf-8")
        ok = subprocess.run(["bash", "-n", str(cli)], capture_output=True)
        assert ok.returncode == 0, ok.stderr
        # uninstall 会 exec 到 $DIR/install.sh；这里没那份脚本，只看它有没有先炸在 local 上
        r = subprocess.run(["bash", str(cli), "uninstall"], capture_output=True, text=True,
                           timeout=60, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
        assert "can only be used in a function" not in (r.stderr + r.stdout)


class TestExplicitPort:
    def test_explicit_port_is_not_silently_changed(self):
        """--port / PORT= 是在说「我就要这个口」，悄悄换掉最糟：摘要、防火墙都照它写的。"""
        text = SCRIPT.read_text(encoding="utf-8")
        body = text[text.index("check_port_free() {"):].split("\n}\n", 1)[0]
        assert "[ \"$PORT_EXPLICIT\" = \"y\" ]" in body
