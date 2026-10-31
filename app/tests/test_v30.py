"""安装脚本的「真装一遍」回归线（v30）。

这一轮的问题都不是读代码看出来的，是**真装了一遍**看出来的：

1. **没 ss/lsof/netstat 的机器上，restart 会把服务停死。**
   minbase、slim、多数 Docker 镜像就是这种状态。端口探测三条路都走不通，
   老代码 `return 1`（当成空闲），于是「等端口放开」的循环立刻放行：
   do_stop 发出 TERM 就宣告停止，do_start 在旧进程还占着端口时启新的，
   新的 bind 失败，屏幕上却只有一句「已后台启动」。
2. **stop_running 的兜底分支是死代码。** 它挂在 `elif` 上，而上面那个分支
   无论如何都会 `rm -f` pid 文件，于是「pid 文件过期 + 端口上还挂着自己那个
   孤儿进程」这种情况永远清不掉，下一次启动撞端口。
3. **do_stop 只发 TERM 就报「已停止」**，进程没退也照报。

所以这里的断言都盯着「能不能落地」：探测函数在没有 ss 的环境里准不准、
生成的命令里还有没有那种「三样都没有就放行」的短路。
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install.sh"


def _block(name: str) -> str:
    """把脚本里某个函数的定义整段抠出来（按 `name() {` 开头、行首 `}` 结尾）。"""
    text = INSTALL.read_text(encoding="utf-8")
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.S | re.M)
    assert m, f"install.sh 里找不到 {name}()"
    return m.group(0)


def _generated_cli() -> str:
    """安装时生成的那份 simpleurl 命令（heredoc 正文，含转义）。

    它是给用户敲的入口，出问题最隐蔽：主脚本改了、生成模板没跟上，
    装到机器上还是旧逻辑。所以直接对模板本身做断言。
    """
    text = INSTALL.read_text(encoding="utf-8")
    start = text.index('${SUDO} tee "$cli"')
    end = text.index("\nEOF\n", start)
    return text[start:end]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestPortProbeWithoutSs:
    """没有 ss / lsof / netstat 时，端口探测必须还是准的。"""

    def run_probe(self, port: int, tmp_path: Path) -> bool:
        # 只留 awk 这三个字本身：ss / lsof / netstat 一个都不可用，
        # 逼它走 /proc/net/tcp 那条路。（别把 awk 也删了——awk 是探测手段，
        # 不是被探测的对象，删了测的就成了「没有 awk 会怎样」。）
        bindir = tmp_path / "empty-bin"
        bindir.mkdir(exist_ok=True)
        awk = shutil.which("awk")
        assert awk, "测试机需要 awk"
        for name in ("ss", "lsof", "netstat"):
            (bindir / name).unlink(missing_ok=True)
        link = bindir / "awk"
        if not link.exists():
            link.symlink_to(awk)
        code = (
            "need_cmd() { command -v \"$1\" >/dev/null 2>&1; }\n"
            + _block("port_in_use")
            + f"\nport_in_use {port} && echo IN-USE || echo FREE\n"
        )
        r = subprocess.run(
            [shutil.which("bash") or "/bin/bash", "-c", code],
            capture_output=True, text=True,
            env={"PATH": str(bindir), "HOME": str(tmp_path)},
            timeout=30,
        )
        assert r.returncode == 0, r.stderr
        assert "not found" not in r.stderr, r.stderr
        return r.stdout.strip() == "IN-USE"

    def test_detects_a_real_listener(self, tmp_path):
        port = _free_port()
        with socket.socket() as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", port))
            srv.listen(1)
            assert self.run_probe(port, tmp_path) is True, "有人在听却说空闲 —— restart 会撞端口"

    def test_reports_free_port_as_free(self, tmp_path):
        assert self.run_probe(_free_port(), tmp_path) is False

    def test_no_silent_free_fallback(self):
        """三个外部命令都没有时，不许一句 `return 1` 就说空闲。

        这是老代码的病根：`else return 1` 让「探测不了」等价于「端口是空的」，
        上面每一条「等端口放开」的循环于是全变成放行。
        """
        block = _block("port_in_use")
        assert "/proc/net/tcp" in block or "/dev/tcp" in block, \
            "没有 ss/lsof/netstat 的路子，探测不了就会一律当成空闲"


class TestGeneratedCliStopsForReal:
    """生成的 simpleurl 命令里，停止与重启得真能做到。"""

    cli = _generated_cli()

    def test_port_probe_does_not_assume_free(self):
        block = re.search(r"^port_listening\(\) \{.*?^\}", self.cli, re.S | re.M)
        assert block, "生成的命令里没有 port_listening()"
        body = block.group(0)
        assert "/proc/net/tcp" in body, "缺少 /proc/net/tcp 兜底"
        # 关键：没有任何一条「什么都探测不到 -> 直接判空闲」的短路
        assert "  fi\n  return 1" not in body.replace("\\", ""), body
        assert body.count("return 0") >= 1

    def test_wait_port_free_waits(self):
        block = re.search(r"^wait_port_free\(\) \{.*?^\}", self.cli, re.S | re.M)
        assert block, "生成的命令里没有 wait_port_free()"
        body = block.group(0)
        assert "port_listening || return 0" in body, "变成无条件放行了"
        assert "sleep 1" in body

    def test_stop_waits_until_the_port_is_released(self):
        body = self.cli
        stop = re.search(r"^do_stop\(\) \{.*?^\}", body, re.S | re.M)
        assert stop
        assert "wait_port_free" in stop.group(0), \
            "do_stop 返回时端口还占着的话，紧接着的 do_start 会 bind 失败"

    def test_stop_escalates_and_reports_failure(self):
        stop = re.search(r"^do_stop\(\) \{.*?^\}", self.cli, re.S | re.M).group(0)
        assert "kill -9" in stop, "TERM 不走要能升级到 KILL"
        assert "没能停掉" in stop, "真的停不掉时要在屏幕上说出来，不能装作停好了"
        assert "已停止（PID" in stop, "停好了也要给个明确的确认"

    def test_restart_does_not_double_wait(self):
        """do_stop 已经等过一轮，restart 里再等一遍只是白等十秒。"""
        assert re.search(r"restart\) do_stop >/dev/null; do_start ;;", self.cli)


class TestMainScriptCleanup:
    """主脚本停旧进程的兜底分支必须真的能走到。"""

    def test_port_holder_fallback_is_reachable(self):
        body = _block("stop_running")
        # 只看真正会被执行的行，注释里提到旧写法不算
        code = "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith("#")
        )
        assert not re.search(r"^\s*elif port_holder_pid", code, re.M), \
            "兜底分支挂在 elif 上永远走不到（上面那个分支必定 rm -f pid 文件）"
        assert "port_holder_pid" in code
        assert 'kill "$PORT_HOLDER_PID"' in code


class TestDoctorStrict:
    """`doctor --strict`：CI 要的是「全绿才继续」。"""

    def run(self, *args, tmp_path: Path):
        env = dict(os.environ)
        env.pop("CNB_TOKEN", None)
        env["SIMPLEURL_HOME"] = str(tmp_path / "install")
        env["PORT"] = str(_free_port())
        return subprocess.run(
            [shutil.which("bash") or "/bin/bash", str(INSTALL), *args],
            capture_output=True, text=True, env=env, timeout=120,
        )

    def test_help_mentions_strict(self, tmp_path):
        r = self.run("--help", tmp_path=tmp_path)
        assert "--strict" in r.stdout

    def test_strict_turns_warnings_into_failure(self, tmp_path):
        loose = self.run("doctor", tmp_path=tmp_path)
        strict = self.run("doctor", "--strict", tmp_path=tmp_path)
        assert "环境体检" in loose.stdout
        # 这台机器的体检结果取决于环境：有提醒时 strict 必须更严，没提醒时两者一致
        if "项提醒，不挡安装" in loose.stdout:
            assert loose.returncode == 0
            assert strict.returncode != 0, "有提醒还判通过，那 --strict 等于没写"
            assert "按 --strict 判为不通过" in strict.stdout
        else:
            assert strict.returncode == loose.returncode

    def test_strict_writes_nothing(self, tmp_path):
        home = tmp_path / "install"
        self.run("doctor", "--strict", tmp_path=tmp_path)
        assert not home.exists()


class TestNoPhantomAssets:
    """默认安装里不该有指向不存在文件的那两个标签。

    以前 base.html 无条件引 site.css / site.js，再挂 onerror 兜底：
    这两个文件默认不存在，于是每开一页都是两个 404，控制台一屏红。
    onerror 也救不回来 —— CSP 的 script-src 只放行 nonce，内联处理器被拦掉，
    控制台反而多两条「violates CSP」。这是真装一遍、真开浏览器才看得见的。
    """

    def test_template_guards_user_assets(self):
        base = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
        assert 'has_user_asset("site.css")' in base
        assert 'has_user_asset("site.js")' in base

    def test_template_has_no_inline_handlers(self):
        """页面里不许出现 onerror/onclick 这类内联处理器：CSP 下它们本来就是死的。"""
        base = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
        assert "onerror=" not in base
        assert "onclick=" not in base

    def test_default_install_renders_without_them(self, client):
        r = client.get("/")
        assert r.status_code == 200
        # 默认安装目录里没有这两个文件，就不该出现在 HTML 里
        assert "/static/site.css" not in r.text
        assert "/static/site.js" not in r.text

    def test_renders_them_when_present(self, client, monkeypatch):
        """用户补了文件就得引上 —— 这个开关不能把用户的定制也一起锁死。"""
        from app import web

        original = web._has_user_asset

        def fake(name: str) -> bool:
            return name in ("site.css", "site.js")

        monkeypatch.setattr(web, "_has_user_asset", fake)
        web.templates.env.globals["has_user_asset"] = fake
        try:
            r = client.get("/")
            assert "/static/site.css" in r.text
            assert "/static/site.js" in r.text
        finally:
            monkeypatch.setattr(web, "_has_user_asset", original)
            web.templates.env.globals["has_user_asset"] = original


class TestFooterDot:
    """窄屏页脚那个分隔点会孤零零挂在行尾，看着像没渲染出来的字符。"""

    def css(self) -> str:
        return (ROOT / "app/static/app.css").read_text(encoding="utf-8")

    def test_dot_is_hidden_on_narrow_screens(self):
        css = self.css()
        m = re.search(r"@media \(max-width: 720px\) \{(.*?)\n\}", css, re.S)
        assert m, "找不到 720px 断点"
        assert ".footer-dot" in m.group(1)
        assert "display: none" in m.group(1)

    def test_template_marks_the_dot(self):
        base = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
        assert 'class="dot footer-dot"' in base
