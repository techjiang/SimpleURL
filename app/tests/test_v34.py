"""安装脚本 v2.7：几条「真装几遍才现形」的坑。

这一轮不开新功能，只把安装链路再压一遍。下面每条都对应一次实测，
断言尽量钉在行为上 —— 改回去要真的变红，而不是「脚本里出现过某段文字」。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "install.sh"
BODY = SCRIPT.read_text(encoding="utf-8")


def func(name: str) -> str:
    lines = BODY.splitlines()
    head = name + "() {"
    start = next(i for i, line in enumerate(lines) if line.startswith(head))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def grab(name: str) -> str:
    """按前缀抓一段源码（脚本里的函数名后面常常带注释）。"""
    lines = BODY.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(name))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


class TestPortProbeIsAdditive:
    """四条探测路要叠加，不能是 elif 链。

    ss 在非 root / 受限容器里可能什么都看不见，而它一装就有 —— 于是「端口被占」
    被判成「空闲」，重启抢跑，失败信息指向别处。任何一条判出「有」都必须算数。
    """

    @pytest.mark.parametrize("name", ["port_in_use", "port_holder_pid"])
    def test_no_elif_chain(self, name):
        body = func(name)
        assert "elif need_cmd" not in body.replace("elif need_cmd fuser", "")
        assert "else\n    # 最后一条" not in body

    def test_install_script_has_proc_fallback(self):
        body = func("port_in_use")
        assert "/proc/net/tcp" in body
        assert "/dev/tcp/127.0.0.1" in body

    def test_generated_cli_probes_stacked(self):
        start = BODY.index("<<'SU_CLI_EOF'")
        tail = BODY[start:].split("\n", 1)[1]
        template = tail[: tail.index("\nSU_CLI_EOF")]
        body = template[template.index("port_listening() {"):]
        body = body[: body.index("\n}")]
        assert "elif command -v" not in body
        assert "/proc/net/tcp" in body

    def test_really_detects_a_listening_port(self, tmp_path):
        """函数真跑一遍：开个监听口，它必须看得见。"""
        import socket

        body = func("port_in_use")
        script = tmp_path / "probe.sh"
        script.write_text(body + "\nport_in_use 8000\n", encoding="utf-8")
        # 8000 在测试环境里可能已被占用：换一个真的空口再开
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            script.write_text(body + f"\nport_in_use {port}\n", encoding="utf-8")
            busy = subprocess.run(["bash", str(script)], capture_output=True, timeout=30)
            assert busy.returncode == 0, "正在监听的口必须判为占用"
        finally:
            s.close()
        script.write_text(body + f"\nport_in_use {port}\n", encoding="utf-8")
        free = subprocess.run(["bash", str(script)], capture_output=True, timeout=30)
        assert free.returncode != 0, "刚放开的端口必须判为空闲"


class TestInstallDirCanonical:
    """`--dir ./site`、`--dir /tmp/link-to-site` 都得收敛成同一个物理路径。

    不收敛的后果不是「路径难看」，而是同一份安装被当成两份：目录里的 simpleurl
    认不出自己（退回 install.sh 硬顶），app/ 被复制进软链深处，.venv 当场失效。
    """

    def test_helper_exists(self):
        assert "_canon_dir() {" in BODY
        assert "_abs_dir() {" in BODY

    def test_canonicalises_before_derived_paths(self):
        """规范化必须排在 LOG_FILE / MARK_FILE 之前，否则派生路径还是旧的。"""
        assert BODY.index('LOG_FILE="$INSTALL_DIR/simpleurl.log"') > BODY.index("_canon_dir() {")

    def test_dir_flag_canonicalises(self):
        i = BODY.index("    --dir)")
        seg = BODY[i: i + 400]
        assert "_canon_dir" in seg

    def test_marker_scan_canonicalises(self):
        """老安装的命令行里可能记着软链路径：顺着它找到目录后同样要规范化。"""
        idx = BODY.index("for _mark in")
        seg = BODY[idx: idx + 800]
        assert "_canon_dir" in seg

    @pytest.mark.parametrize("shape", ["symlink", "relative"])
    def test_same_dir_for_symlink_and_relative(self, tmp_path, shape):
        helpers = grab("_abs_dir() {") + "\n" + grab("_canon_dir() {")
        real = tmp_path / "real"
        real.mkdir()
        if shape == "symlink":
            link = tmp_path / "link"
            link.symlink_to(real)
            arg = str(link)
            want = str(real.resolve())
        else:
            arg = "real"
            want = str(real.resolve())
        script = tmp_path / "canon.sh"
        script.write_text(helpers + f'\ncd "{tmp_path}"\n_canon_dir "{arg}"\n', encoding="utf-8")
        out = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == want


class TestReexecKeepsArgs:
    """换壳重跑要把「用户说了什么」带过去。

    之前一个都没传：A 轮按 --no-health 跳过验收，B 轮照样等 60 秒；A 轮认出的
    端口 B 轮当成默认值，发现被自己占着又「自动换口」—— 重装把服务搬到了隔壁。
    """

    @pytest.fixture(scope="class")
    def seg(self):
        i = BODY.index('_reexec_args=("$REEXEC_ACTION" --yes)')
        return BODY[i: i + 1200]

    @pytest.mark.parametrize(
        "flag",
        ["--no-health", "--force", "--strict", "--port", "--branch"],
    )
    def test_forwards(self, seg, flag):
        assert flag in seg, f"换壳重跑没带上 {flag}"

    def test_forwards_port_as_explicit(self):
        """端口要连着「这是显式指定」一起传：只传值，B 轮仍会把它当默认值换掉。"""
        i = BODY.index("exec bash \"$fresh_script\"")
        seg = BODY[max(0, i - 900): i]
        assert 'PORT="$PORT"' in seg
        assert 'PORT_EXPLICIT="$PORT_EXPLICIT"' in seg


class TestInstalledCodeSelfCheck:
    """装进去的那份代码要能被自己的解释器 import，版本还得对得上源码。

    这条抓的是「装了个寂寞」：脚本靠标记文件里的 code_version 判断「已经最新」，
    而那个值正是上一轮自己写进去的 —— 复用了旧标记，屏幕上就会说「代码未变动」，
    哪怕落到目录里的是另一份代码。
    """

    def test_function_exists(self):
        body = func("check_installed_code")
        assert "import app" in body
        assert "docker exec" in body

    def test_called_during_install(self):
        assert "check_installed_code || die" in BODY

    def test_check_rejects_mismatch(self, tmp_path):
        """用一个写着别的版本的假包跑一遍：必须判失败。"""
        body = func("check_installed_code")
        pkg = tmp_path / "site"
        (pkg / "app").mkdir(parents=True)
        (pkg / "app" / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
        (pkg / "app" / "main.py").write_text("", encoding="utf-8")
        vend = pkg / ".venv" / "bin"
        vend.mkdir(parents=True)
        (vend / "python").write_text("#!/usr/bin/env python3\nprint('1.0.0')\n", encoding="utf-8")
        (vend / "python").chmod(0o755)
        script = tmp_path / "check.sh"
        script.write_text(
            "set -euo pipefail\n"
            "warn() { echo \"! $*\" >&2; }\n"
            "ok() { echo \"✓ $*\"; }\n"
            f'INSTALL_DIR="{pkg}"\nRUNTIME="python"\nSERVICE_NAME="simpleurl"\n'
            # remote_version 读的就是 app/__init__.py 里的 __version__
            + grab("remote_version() {") + "\n"
            + 'RUNTIME_OVERRIDE=""\n'
            + body
            + "\ncheck_installed_code\n",
            encoding="utf-8",
        )
        r = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60)
        assert r.returncode != 0
        assert "9.9.9" in (r.stdout + r.stderr)


class TestPasswordIsVerified:
    """`simpleurl password --reset` 改完要能真登进去，而不是「屏幕上写着新密码」。"""

    def test_verify_login_exists(self):
        body = func("verify_login")
        assert "/api/admin/login" in body
        assert "su_csrf" in body

    def test_set_password_calls_it(self):
        body = func("set_password")
        assert "verify_login" in body

    def test_missing_curl_is_not_silent_pass(self):
        """没 curl 时不能装作「验过了」—— 那正是这次 CI 挂掉的根因。"""
        body = func("verify_login")
        assert "command -v curl" in body
        assert "跳过了新密码的登录验证" in body

    def test_csrf_read_by_field_not_position(self):
        """cookie 文件按字段名取，不按位置取：多一个属性整行就错开一格。"""
        body = func("verify_login")
        assert 'awk -F' in body and '$6 == "su_csrf"' in body

    def test_login_check_uses_real_credentials(self, tmp_path):
        """真起一个假站点，验一下判定逻辑：200 通过、401 失败。"""
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                # 真实站点用的是 HttpOnly 的 su_csrf cookie；HTTPOnly 属性里正好
                # 含子串 "HTTP"，awk 那个字段位就整体错开一格，取不到令牌 ——
                # 于是这条函数在「像真的」和「像假的」之间表现完全不同。
                if self.path == "/":
                    self.send_response(200)
                    self.send_header("Set-Cookie", "su_csrf=tok123; HttpOnly; Path=/")
                    self.end_headers()
                    self.wfile.write(b"<html></html>")
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                self.server.posts = getattr(self.server, "posts", 0) + 1
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                # 密码对不对，直接看请求体里有没有那串 —— 不同 Python 版本的
                # json 库对这些字节的处理不完全一样，用它当判据，测试就不再
                # 「本地过、CI 挂」
                code = 200 if b"right-one" in raw else 401
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            body = func("verify_login")
            # 这份脚本必须能自己跑起来：被抠出来的函数里只要有一处用了外部变量，
            # 本地就可能因为环境里的残留而「过」，CI 上则从头到尾静默失败。
            # 下面的断言就是那条底线 —— 起一个假站，通过必须是 200、不通过必须失败。
            assert "PORT" in body
            script = tmp_path / "v.sh"
            script.write_text(
                "set -uo pipefail\nsay() { echo \"$*\"; }\n"
                f'PORT="{port}"\n' + body + "\nverify_login admin right-one\n",
                encoding="utf-8",
            )
            # 这条用例要真发 HTTP 请求，得有 curl。CI 镜像里未必装 —— 那种情况下
            # 跳过，而不是让「没有 curl 就走通过分支」骗出一片绿。
            # （缺 curl 时的正确行为另有 test_missing_curl_is_not_silent_pass 钉住。）
            env = dict(os.environ)
            if subprocess.run(["bash", "-c", "command -v curl"], env=env,
                              capture_output=True).returncode != 0:
                pytest.skip("这台机器没有 curl，验不了真登录")
            good = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                                  timeout=60, env=env)
            # 只判 rc 不够：取不到 CSRF、连不上站点、curl 根本没装，这几种
            # 「假通过」也是一样的 rc。真验成功必须看见站点回的那 200。
            assert good.returncode == 0, good.stdout + good.stderr
            assert "✓" in good.stdout, good.stdout + good.stderr
            assert getattr(srv, "posts", 0) == 1, f"登录请求没打到站点上：posts={getattr(srv, 'posts', 0)}"
            script.write_text(
                "set -uo pipefail\nsay() { echo \"$*\"; }\n"
                f'PORT="{port}"\n' + body
                + "\nverify_login admin wrong-one; echo \"rc=$?\"\n",
                encoding="utf-8",
            )
            bad = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                                 timeout=60, env=env)
            # 脚本末尾那句 echo 会把退出码冲成 0，所以看输出里的 rc，不看 returncode
            assert "rc=1" in bad.stdout, (
                # 挂了要把现场带出来：README 里那句「返回 0 就算过」的断言本身
                # 也可能被别的东西带偏，光看 rc 猜不出问题在哪
                f"stdout={bad.stdout!r} stderr={bad.stderr[-500:]!r} "
                f"script_tail={script.read_text()[-200:]!r} "
                f"posts={getattr(srv, 'posts', 0)}"
            )
        finally:
            srv.shutdown()


class TestDockerPathIsVerified:
    """Docker 装完也要真验一遍：容器起来了不等于站能用。"""

    def test_calls_health_and_verify(self):
        body = func("install_docker_mode")
        assert "wait_health" in body
        assert "verify_end_to_end" in body
        assert "rollback_or_die" in body

    def test_respects_no_health(self):
        body = func("install_docker_mode")
        assert 'SKIP_HEALTH" != "y"' in body


class TestSiblingInstalls:
    """两份安装共用一个命令名：谁倒霉，谁的站就「凭空消失」。

    后装的那份会把 /usr/local/bin/simpleurl 覆盖成自己的；先装那份只剩目录里的
    影子命令。卸载后装那份时，如果只顺着全局命令找「隔壁」，命令被删掉、
    先装那份却接不回来 —— 站点还在跑，人以为东西没了。
    """

    def test_records_siblings(self):
        # install_cli 的正文里有 heredoc：用字符串定位，别让「遇到行首 } 就收」骗了
        assert 'cp -f \'$cli\' \'$INSTALL_DIR/simpleurl\'' in BODY
        assert "record_siblings" in BODY
        body = func("record_siblings")
        # 只看全局命令不够：先装那份已经不是全局命令了
        assert "/simpleurl" in body

    def test_sibling_file_declared(self):
        assert 'SIBLING_FILE="$INSTALL_DIR/.simpleurl-siblings"' in BODY

    def test_uninstall_uses_sibling_record(self):
        body = func("do_uninstall")
        assert "SIBLING_FILE" in body
        assert "still_there" in body

    def test_uninstall_does_not_glob_home_for_markers(self):
        """`for d in "$HOME"/*/`：没匹配时字面量 "/root/*" 会被当成目录名，
        看着扫过了、其实什么也没扫到。"""
        body = func("do_uninstall")
        assert '"$HOME"/*/.' not in body

    def test_uninstall_no_local_outside_function(self):
        """do_uninstall 是顶层函数，local 合法；但命令副本里的 case 分支不是。"""
        body = func("do_uninstall")
        assert "local bindir" not in body, "bindir 在命令副本里已声明过全局变量"

    def test_command_handed_back_goes_where_permissions_allow(self, tmp_path):
        """非 root 时往 /usr/local/bin 写必然失败，失败还不出声 = 命令凭空消失。"""
        body = func("do_uninstall")
        assert "bindir=\"/usr/local/bin\"" in body
        assert "bindir=\"$HOME/.local/bin\"" in body

    def test_sibling_record_really_written(self, tmp_path):
        """真跑一遍：隔壁目录里那份影子命令会被认出来。"""
        body = func("record_siblings")
        sibling = tmp_path / "su-a"
        sibling.mkdir()
        (sibling / ".simpleurl-managed").write_text("runtime=python\n", encoding="utf-8")
        (sibling / "simpleurl").write_text('#!/usr/bin/env bash\nDIR="' + str(sibling) + '"\n', encoding="utf-8")
        (sibling / "simpleurl").chmod(0o755)
        me = tmp_path / "su-b"
        me.mkdir()
        script = tmp_path / "sib.sh"
        script.write_text(
            "set -uo pipefail\n"
            f'HOME="{tmp_path / "home"}"\n'
            f'INSTALL_DIR="{me}"\n'
            f'SIBLING_FILE="{tmp_path / "sib"}"\n'
            "ok() { :; }\n"
            + body
            + "\nrecord_siblings\ncat \"$SIBLING_FILE\"\n",
            encoding="utf-8",
        )
        # 把影子命令扫到的地方换成 tmp_path：真实现扫的是 /tmp、$HOME、/opt 那一圈
        text = script.read_text().replace(
            'for cmd in $(ls -d /tmp/*/simpleurl', f'for cmd in $(ls -d {tmp_path}/*/simpleurl'
        )
        script.write_text(text, encoding="utf-8")
        r = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60)
        assert f"dir={sibling}" in r.stdout, r.stdout + r.stderr


class TestCheckPortFreeWording:
    """「会改用 8081」这种话必须和实际行为一致。"""

    def test_explicit_port_does_not_promise_switch(self):
        body = func("doctor")
        assert "你指定了端口，不会自动换" in body

    def test_holder_pid_is_reported(self):
        body = func("check_port_free")
        assert "PID ${PORT_HOLDER_PID}" in body


class TestScriptStillSane:
    def test_bash_syntax(self):
        r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_version_bumped(self):
        assert 'VERSION="2.7.0"' in BODY

    def test_no_placeholder_left_in_template(self):
        """渲染占位只剩三个，且都被替换过 —— 少一个就落到磁盘上一串 __SU_*__。"""
        start = BODY.index("<<'SU_CLI_EOF'")
        tail = BODY[start:].split("\n", 1)[1]
        template = tail[: tail.index("\nSU_CLI_EOF")]
        found = set(re.findall(r"__SU_[A-Z_]+__", template))
        assert found == {"__SU_DIR__", "__SU_NAME__", "__SU_SCRIPT_VERSION__"}


class TestPortOwnership:
    """端口上那个人是不是「自己人」，判错两边都疼。

    判宽了（只看 pid 文件写了它）：两份安装撞同一个端口时，B 的 pid 文件里那个号
    早被 A 重启后复用，B 一停把 A 干掉了。
    判窄了（只认目录）：安装目录被 rm -rf 而进程还活着，谁的记录都对不上，
    这个端口从此卡死，重装只能一遍遍 bind 失败。
    """

    def test_helper_exists_and_is_used(self):
        assert "port_owner_ours() {" in BODY
        for place in ("check_port_free", "doctor", "stop_running"):
            assert "port_owner_ours" in func(place), place

    def test_stop_refuses_foreign_process(self):
        body = func("stop_running")
        assert "那不是本目录的服务，不动它" in body
        # pid 文件只是线索，得再拿命令行 / cwd 核一次归属
        assert 'pid_in_this_dir "$pid"' in body
        assert "stop_pid" in body

    def test_stop_cleans_orphan_of_this_dir(self):
        assert "PORT_ORPHAN_PID" in func("stop_running")

    def test_fresh_install_also_cleans_port(self):
        """「删掉目录再装回去」不该卡在 bind 失败上。"""
        body = func("do_install")
        assert "elif port_in_use" in body

    def test_cli_stop_also_verifies(self):
        start = BODY.index("<<'SU_CLI_EOF'")
        tail = BODY[start:].split("\n", 1)[1]
        template = tail[: tail.index("\nSU_CLI_EOF")]
        assert "pid_is_ours() {" in template
        assert 'pid_is_ours "$pid"' in template

    @staticmethod
    def _harness(tmp_path, setup, fake_pid=None):
        """把两个「判归属」的函数抠出来，配好桩再跑。

        桩必须排在被测函数之前：bash 先看见哪个定义就用哪个。
        只抠 port_owner_ours —— 真实现里 port_holder_pid 会去问内核，桩才有意义。
        """
        # 只取一个：被测函数只有 port_owner_ours。port_holder_pid 由桩顶上，
        # 真实现会把机器上正在跑的那个服务认出来，测试就不受控了。
        helpers = grab("port_owner_ours() {")
        site = tmp_path / "site"
        site.mkdir(exist_ok=True)
        script = tmp_path / "own.sh"
        script.write_text(
            "set -uo pipefail\n"
            'need_cmd() { command -v "$1" >/dev/null 2>&1; }\n'
            'port_holder_pid() { PORT_HOLDER_PID="${FAKE_PID:-$$}"; return 0; }\n'
            f'INSTALL_DIR="{site}"\n'
            f'MARK_FILE="{site / ".simpleurl-managed"}"\n'
            'PORT="18960"\n'
            + setup(site)
            + "\n# ---- 被测函数 ----\n"
            + helpers
            + "\nport_owner_ours \"$PORT\" && echo OURS || echo NOT-OURS\n"
            + 'echo "orphan=${PORT_ORPHAN_PID:-}"\n',
            encoding="utf-8",
        )
        env = {**os.environ}
        if fake_pid is not None:
            env["FAKE_PID"] = str(fake_pid)
        r = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60, env=env)
        out = (r.stdout + r.stderr).split()
        # 取最后一次判定的结果（桩自己也可能调用被测函数）
        verdict = "OURS" if "OURS" in out else "NOT-OURS"
        orphan = next((w for w in out if w.startswith("orphan=")), "orphan=")
        return verdict + " " + orphan

    def test_foreign_listener_is_not_ours(self, tmp_path):
        """端口上是别人（命令行与 cwd 都不指向本目录）：判不是自己人。"""
        out = self._harness(tmp_path, lambda site: "")
        assert "NOT-OURS" in out, out

    def test_pid_file_alone_is_not_enough(self, tmp_path):
        """pid 文件里写着它，但命令行 / cwd 对不上 —— 那是被复用的号，不能杀。"""

        def setup(site):
            (site / "simpleurl.pid").write_text(str(os.getpid()), encoding="utf-8")
            # 连标记文件都说「这套安装用这个端口」，也不该只听记录
            (site / ".simpleurl-managed").write_text("port=18960\n", encoding="utf-8")
            return ""

        assert "NOT-OURS" in self._harness(tmp_path, setup)

    def test_dir_gone_but_process_left(self, tmp_path):
        """安装目录没了、记录也没了，但 cwd 指向它：认成孤儿，好让安装去收。"""

        def setup(site):
            # cwd 带 deleted 后缀：目录被 rm -rf 掉了，进程还活着
            return f'readlink() {{ printf "%s (deleted)" "{site}"; }}\n'

        # 用 1 号进程当「还活着的那一个」：它的 /proc 一定存在，
        # 不必赌测试进程自己的 pid 会不会被谁复用
        out = self._harness(tmp_path, setup, fake_pid=1)
        assert "orphan=1" in out, out


class TestScriptStillSane:
    def test_bash_syntax(self):
        r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_version_bumped(self):
        assert 'VERSION="2.7.0"' in BODY

    def test_no_placeholder_left_in_template(self):
        """渲染占位只剩三个，且都被替换过 —— 少一个就落到磁盘上一串 __SU_*__。"""
        start = BODY.index("<<'SU_CLI_EOF'")
        tail = BODY[start:].split("\n", 1)[1]
        template = tail[: tail.index("\nSU_CLI_EOF")]
        found = set(re.findall(r"__SU_[A-Z_]+__", template))
        assert found == {"__SU_DIR__", "__SU_NAME__", "__SU_SCRIPT_VERSION__"}
