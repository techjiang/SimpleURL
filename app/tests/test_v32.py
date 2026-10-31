"""v2.5.0 一键安装脚本：这一轮真装出来才现形的几处。

每条都对应一次真实事故。断言写得尽量具体 —— 纯「脚本里出现了某段文字」
挡不住回归，得让它在改回去时真的变红。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "install.sh"
BODY = SCRIPT.read_text(encoding="utf-8")


def func(name: str) -> str:
    """把某个顶层函数原文抠出来：从 'name() {' 到行首的 '}'。"""
    lines = BODY.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def cli_template() -> str:
    """install_cli 里那段 heredoc 正文。"""
    start = BODY.index("<<'SU_CLI_EOF'")
    head, tail = BODY[start:].split("\n", 1)
    return tail[: tail.index("\nSU_CLI_EOF")]


class TestCliTemplateIsLiteral:
    """生成的 simpleurl 命令：模板必须一字不展开。

    上一版是 <<EOF 加「每个 $ 都要转义、正文不许出现反引号」的约定。约定守不住：
    version 那行里嵌套的 $(cat ... | sed ...) 装了两层反斜杠还是被外层吃掉，
    落盘的命令里子命令整段消失 —— simpleurl version 于是永远打「代码 」后面空白，
    而脚本作者以为它是好的。
    """

    def test_heredoc_is_quoted(self):
        assert "<<'SU_CLI_EOF'" in BODY, "模板 heredoc 必须带引号，正文一个字节都不展开"

    def test_template_has_no_escapes(self):
        template = cli_template()
        assert "\\$" not in template, "正文已是字面量，出现反斜杠美元说明有人照旧习惯又写了一遍"
        assert "\\`" not in template

    def test_template_parses_alone(self):
        r = subprocess.run(["bash", "-n"], input=cli_template(), capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_no_placeholder_left_after_render(self):
        """渲染后不该残留 __SU_*__。占位只有三个，且都被 sed 替换。"""
        template = cli_template()
        placeholders = set(re.findall(r"__SU_[A-Z_]+__", template))
        assert placeholders == {"__SU_DIR__", "__SU_NAME__", "__SU_SCRIPT_VERSION__"}
        for name in placeholders:
            assert f"s|{name}|" in BODY, f"{name} 没有替换规则"

    def test_version_line_uses_variable(self):
        """version 那一行不能再自己拼子命令：那正是被吃掉的那段。"""
        template = cli_template()
        line = next(l for l in template.splitlines() if l.strip().startswith("version)"))
        assert "$CODE_VER" in line and "$SCRIPT_VER" in line
        assert "__version__" not in line


class TestPortHolderDetection:
    """端口被谁占着 —— 不能只有 ss 一条路。"""

    def test_falls_back_when_ss_has_no_pid(self):
        """ss 认不出 pid 时必须继续往下探。"""
        body = func("port_holder_pid")
        assert "/proc/net/tcp" in body, "要有内核兜底：slim/容器里通常没有 ss/lsof"
        assert "find /proc/" in body, "要用 socket inode 反查 pid"
        # ss 分支不能是「有输出就 return」，否则 /proc 永远轮不到
        ss_branch = body[body.index("need_cmd ss"): body.index("/proc/net/tcp")]
        assert "return 1" not in ss_branch, "ss 拿不到 pid 时不该直接判失败"

    def test_recognizes_listener(self):
        """真起一个监听进程，能反查出它的 pid。"""
        script = (
            'source <(sed -n "/^port_holder_pid()/,/^}/p" "$0")\n'
            'source <(sed -n "/^need_cmd()/,/^}/p" "$0")\n'
            'port_holder_pid "$1" && echo "pid=$PORT_HOLDER_PID" || echo none\n'
        )
        holder = subprocess.run(
            ["python3", "-c",
             "import socket,sys,time\n"
             "s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(1)\n"
             "print(s.getsockname()[1], flush=True); time.sleep(5)"],
            capture_output=True, text=True, timeout=20,
        )
        # 上面那条是同步的，会等满 5 秒；改成后台进程拿端口
        import socket as _s
        sock = _s.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            r = subprocess.run(
                ["bash", "-c", script, str(SCRIPT), str(port)],
                capture_output=True, text=True, timeout=30,
            )
            assert r.stdout.strip().startswith("pid="), f"没认出监听 {port} 的进程：{r.stdout} {r.stderr}"
        finally:
            sock.close()

    def test_ss_without_pid_line(self):
        """造一个「只有 ss 但 ss 不给 pid」的环境：必须还能判出占用者。"""
        script = (
            'source <(sed -n "/^port_holder_pid()/,/^}/p" "$0")\n'
            'need_cmd() { [ "$1" = ss ]; }\n'          # 假装装的是 ss
            'ss() { echo "LISTEN 0 4096 0.0.0.0:$1 0.0.0.0:*"; }\n'  # 不给 pid=
            'port_holder_pid "$1" && echo "pid=$PORT_HOLDER_PID" || echo none\n'
        )
        import socket as _s
        sock = _s.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            r = subprocess.run(
                ["bash", "-c", script, str(SCRIPT), str(port)],
                capture_output=True, text=True, timeout=30,
            )
            assert r.stdout.strip().startswith("pid="), (
                "ss 不给 pid 时没有退到 /proc 兜底：" + r.stdout + r.stderr
            )
        finally:
            sock.close()


class TestReinstallKeepsItsOwnCli:
    """装了多份时，每份都得能找到「属于自己的」命令。"""

    def test_forward_checks_cli_owner(self):
        body = func("forward_subcommand")
        assert "cdir" in body, "转发前要核对命令指向的安装目录"
        assert "$INSTALL_DIR/simpleurl" in body, "安装目录里的影子要优先用"

    def test_install_writes_shadow_cli(self):
        assert '"$INSTALL_DIR/simpleurl"' in BODY, "安装时要在安装目录里留一份 simpleurl 影子"

    def test_uninstall_removes_shadow(self):
        """影子命令在安装目录里，目录被整个删掉，它跟着走。"""
        body = func("do_uninstall")
        assert 'rm -rf "${INSTALL_DIR:?}"' in body
        # 但「只删自己那份」的判据必须留着，别退回无条件删两个路径
        assert 'if [ "$cdir" = "$INSTALL_DIR" ]' in body

    def test_foreign_cli_is_reported(self):
        """只装了别的目录那一份时，要明说而不是静悄悄转过去。"""
        body = func("forward_subcommand")
        assert "机器上的 simpleurl 指向" in body


class TestAccessModeNames:
    """安装脚本问的访问模式，必须是站点真正认的那两个值。"""

    def test_only_open_and_admin(self):
        assert 'ask "访问模式 open' in BODY
        asked = BODY.split('ask "访问模式', 1)[1][:160]
        assert "invite" not in asked, "站点只认 open / admin，邀请码模式早没了"

    def test_verify_guest_path_uses_open(self):
        body = func("verify_end_to_end")
        assert 'ACCESS_MODE" = "open"' in body, "开发模式下访客建链的验收要用站点认的值"
        assert 'ACCESS_MODE" = "public"' not in body


class TestDevFilesStayOut:
    """开发文件不该装进安装目录。"""

    def test_not_in_source_entries(self):
        entries = re.search(r'^SOURCE_ENTRIES="(.*?)"', BODY, re.S | re.M).group(1)
        for dev in ("requirements-dev.txt", "pytest.ini", ".cnb.yml"):
            assert dev not in entries, f"{dev} 不是运行所需，不该跟着装"

    def test_cleanup_removes_dev_entries(self):
        assert "$DEV_ENTRIES" in func("clear_source")

    def test_reuse_path_also_cleans(self):
        """「代码没变、跳过下载」那条捷径也要顺手清。"""
        idx = BODY.index("本地版本与安装目录一致，跳过下载")
        assert "$DEV_ENTRIES" in BODY[idx: idx + 900]


class TestDoctorPortMessage:
    """体检对端口的说法要分得清「自己的」和「别人的」。"""

    def test_has_own_service_branch(self):
        body = func("doctor")
        assert "由上次安装的 SimpleURL 自己占着" in body
        assert "is_our_service" in body

    def test_reinstall_uses_recorded_port(self):
        """已有安装上重跑要沿用记录里的端口，别拿默认值去体检。"""
        assert "PORT_EXPLICIT" in BODY
        idx = BODY.index('PORT_EXPLICIT" ] && [ -f "$MARK_FILE" ]')
        assert "port=" in BODY[idx: idx + 400]


class TestSummarySaysWhatChanged:
    """装完得说清这轮到底动了什么。"""

    def test_reports_version_change(self):
        body = func("summary")
        assert "PREV_CODE_VERSION" in body
        assert "代码已更新" in body
        assert "本轮代码未变动" in body


class TestAuthHeaderScope:
    """令牌只发给 OpenAPI 域名。

    cnb.cool 网关见到 Authorization 会直接 400（报文 42 字节），curl -f 判失败。
    而「网页域名」恰好排在候选地址第一位，于是设了 CNB_TOKEN 的机器取远端脚本、
    探远版本一路全挂，屏幕上只剩「探不到远端版本」，看着像网络问题。
    """

    def test_token_only_for_api_host(self):
        body = func("auth_header")
        assert 'API_HOST' in body, "要靠 API_HOST 判断，而不是「带 api. 字样」"
        assert "return 0" in body

    def test_fetch_passes_url(self):
        assert 'auth_header "$1"' in func("fetch")


class TestSelfUpdateNeverDowngrades:
    """换壳重跑只在远端更新时才做。

    装的是刚改过的本地版本时，远端 main 反而是旧的。老代码只比「内容不同」，
    于是把本地这份原地降级：跑完一轮「升级」，落盘的脚本比开始前还老。
    """

    def test_compares_versions_not_bytes(self):
        idx = BODY.index("换成远端最新版再继续")
        window = BODY[idx - 600: idx + 60]
        assert "version_lt" in window, "换壳前要比较版本，不能只看内容是否相同"

    def test_local_source_shortcut_respects_version(self):
        """脚本比目录里的代码新时，不能借口「脚本在安装目录里」跳过下载。"""
        body = func("stage_source")
        assert "version_lt" in body
        assert "照常取源码" in body


class TestCliPermissions:
    """生成的 simpleurl 必须能被执行。

    `cat > file` 沿用已存在文件的权限，某些 umask / mktemp 环境下是 600：
    root 敲着没事（root 不看权限位），换个运维账号就 Permission denied，
    报错长得像「文件坏了」。
    """

    def test_explicit_mode(self):
        assert "chmod 755" in BODY
        idx = BODY.index("chmod 755")
        assert "cli" in BODY[idx: idx + 120]

    def test_shadow_copy_also_755(self):
        """func() 只切到最后一个 }，模板正文很长——直接看全文里影子那一行。"""
        line = next(
            l for l in BODY.splitlines()
            if "INSTALL_DIR/simpleurl" in l and l.lstrip().startswith("$SUDO")
        )
        assert "chmod 755" in line, "影子那份也要给执行位"
