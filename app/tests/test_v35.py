"""v2.7.0 安装脚本：装完之后才会踩到的几处。

前两轮盯的是「装得出、装得对」。这一轮把镜头挪到装完之后：重复装第二份、
影子命令没了、备份路径写不进去、端口被别人占着还显式指定了它。
每一条都是真实跑出来的，不是读代码想出来的。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "install.sh"
BODY = SCRIPT.read_text(encoding="utf-8")


def _strip_comment(line: str) -> str:
    """去掉行尾注释（# 前面的才算代码），字符串里的 # 不动。"""
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "#":
            break
        out.append(ch)
    return "".join(out)


def _balanced(text: str, start: int) -> str:
    """从第 start 行开始，按花括号配平，返回整个块。"""
    lines = text.splitlines()
    depth = 0
    for i in range(start, len(lines)):
        code = _strip_comment(lines[i])
        depth += code.count("{") - code.count("}")
        if depth == 0 and i > start:
            return "\n".join(lines[start:i + 1])
    raise AssertionError(f"第 {start} 行起的块没配平")


def func(name: str) -> str:
    """抠出某个函数的完整原文。

    不能用「下一个行首是 } 的行」当结尾 —— 函数体里但凡有一个 if/for 的收尾
    也是行首的 }，抠出来就是半截，塞进测试脚本里直接语法错。所以老老实实
    数花括号：从 `name() {` 开始，配平为止。
    """
    lines = BODY.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    return _balanced(BODY, start)


def func_in(text: str, name: str) -> str:
    """在任意文本（比如解出来的 CLI 模板）里抠函数。"""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    return _balanced(text, start)


def cli_template() -> str:
    """install_cli 里那段 heredoc 正文。

    结尾要认「行首且顶格的 SU_CLI_EOF」——注释里也出现过这个字符串，
    按第一次出现截会截到注释上，后面拼出来的就是半截脚本。
    """
    start = BODY.index("<<'SU_CLI_EOF'")
    tail = BODY[start:].split("\n", 1)[1]
    return tail[: tail.index("\nSU_CLI_EOF\n")]


def run_bash(script: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script, "bash", *args],
        capture_output=True, text=True, timeout=30, env=env and {**__import__("os").environ, **env},
    )


class TestForwardNeverExecsItself:
    """影子命令不在时，`./install.sh status` 以前会 exec 到自己。

    exec 到自己是永久的：进程不 fork、不输出、也不退出 —— 屏幕上什么都不发生。
    实测在一个没有影子命令的安装目录里敲 `bash ./install.sh status`，
    它在那儿躺到超时。判「同一份文件」不能用路径字符串，得用 inode。
    """

    def test_helper_exists_and_compares_inode(self):
        body = func("is_same_file")
        assert "ls -di" in body, "必须按 inode 比，而不是比路径字符串"
        assert "$a" in body and "$b" in body

    def test_detects_same_file_through_symlink_and_relative_path(self, tmp_path):
        f = tmp_path / "install.sh"
        f.write_text("#!/usr/bin/env bash\ntrue\n")
        link = tmp_path / "alias.sh"
        link.symlink_to(f)
        script = func("is_same_file") + f'\nis_same_file "{f}" "{link}" && echo SAME\n'
        out = run_bash(script).stdout
        assert "SAME" in out, "符号链接指向同一个文件，必须判成同一个"

    def test_two_real_files_are_not_the_same(self, tmp_path):
        a, b = tmp_path / "a.sh", tmp_path / "b.sh"
        a.write_text("x"); b.write_text("x")
        script = func("is_same_file") + f'\nis_same_file "{a}" "{b}" && echo SAME || echo DIFF\n'
        assert "DIFF" in run_bash(script).stdout

    def test_forward_guards_against_self(self):
        body = func("forward_subcommand")
        assert "is_same_file" in body, "转发前必须排除「就是我自己」这一条"
        guard = body[body.index("is_same_file") - 80: body.index("is_same_file") + 60]
        assert "exec bash" not in guard.split("is_same_file")[0]

    def test_missing_shadow_gives_up_instead_of_looping(self, tmp_path):
        """端到端：目录里有标记、没有影子命令、机器上那份指向别处 → 必须报错退出。"""
        d = tmp_path / "site"
        d.mkdir()
        (d / ".simpleurl-managed").write_text("runtime=python\nport=1\n")
        (d / "install.sh").write_text(SCRIPT.read_text(encoding="utf-8"))
        script = (
            f'INSTALL_DIR="{d}"\n'
            f'MARK_FILE="{d}/.simpleurl-managed"\n'
            f'SELF_PATH="$INSTALL_DIR/install.sh"\n'
            f'HOME="{tmp_path}"\n'
            'YELLOW=""; RST=""\n'
            + func("is_same_file") + "\n"
            + func("forward_subcommand") + "\n"
            + "forward_subcommand status\n"
            + 'echo "RC=$?"\n'
        )
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)
        assert r.returncode != 124, "不许卡死"
        assert "RC=" in r.stdout, "必须走完、给出结论"


class TestShadowCommandIsTheOnlyTrust:
    """`/usr/local/bin/simpleurl` 指向隔壁时，脚本以前一句话不说就转了过去。

    症状最迷惑：`bash /a/install.sh status` 打印的是 /b 的站点地址。
    现在判据只剩一条 —— 命令里记的 DIR 必须等于这个安装目录；
    读不出 DIR、或者 DIR 是别人，都不采用。
    """

    def test_empty_dir_is_not_accepted(self):
        body = func("forward_subcommand")
        line = next(l for l in body.splitlines() if 'cdir" = "$INSTALL_DIR"' in l)
        assert '-n "$cdir"' in line, "空 DIR 不能被当成「自己的命令」"

    def test_foreign_dir_is_reported_not_used(self):
        body = func("forward_subcommand")
        assert "foreign" in body
        assert "没有自己的 simpleurl 命令" in body

    def test_pointer_from_other_install_is_refused(self, tmp_path):
        site = tmp_path / "site"
        other = tmp_path / "other"
        site.mkdir(); other.mkdir()
        (site / ".simpleurl-managed").write_text("runtime=python\nport=1\n")
        fake = tmp_path / "bin" / "simpleurl"
        fake.parent.mkdir()
        fake.write_text(f'#!/usr/bin/env bash\nDIR="{other}"\necho FROM_OTHER\n')
        fake.chmod(0o755)
        script = f'''
INSTALL_DIR="{site}"
MARK_FILE="{site}/.simpleurl-managed"
SELF_PATH="{SCRIPT}"
HOME="{tmp_path}"
''' + func("is_same_file") + func("forward_subcommand") + f'''
forward_subcommand status || true
'''
        r = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=20,
            env={**__import__("os").environ, "PATH": f"{fake.parent}:/usr/bin:/bin"},
        )
        assert "FROM_OTHER" not in r.stdout, "不许把请求悄悄转给别的安装目录"


class TestBackupTellsTheTruth:
    """备份失败不能只留一条 mkdir 的原生报错。

    `simpleurl backup /proc/nope/x.tar.gz` 以前退出码是 2 但屏幕上只有
    `mkdir: cannot create directory`，人以为备份成了 —— 真有备份需求的那天
    去翻文件才发现是空的。
    """

    def test_backup_checks_dir_and_tar(self):
        body = cli_template()
        seg = func_in(body, "do_backup")
        assert "备份目录不可写" in seg
        assert "打包失败" in seg
        assert "return 2" in seg

    def test_unwritable_dir_prints_chinese_reason(self, tmp_path):
        body = cli_template()
        seg = func_in(body, "do_backup")
        script = f'''
DIR="{tmp_path}"
RUNTIME=python
warn() {{ printf "! %s\\n" "$*"; }}
say() {{ printf "%s\\n" "$*"; }}
''' + seg + f'''
do_backup /proc/definitely-not-here/x.tar.gz
echo "RC=$?"
'''
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)
        assert "备份目录不可写" in r.stdout + r.stderr
        assert "RC=2" in r.stdout


class TestTokenOnlyGoesToApiHost:
    """令牌只发给 API 域名，而且必须是「主机名等于」，不是「字符串里出现过」。

    `case "$url" in *"$API_HOST"*` 这种写法会把
    `https://evil.example/?x=api.cnb.cool` 判成 API 域名 —— 令牌就送出去了。
    URL 是能被环境变量改写的（REPO_URL / REPO_TARBALL / GIT_HOST）。
    """

    def test_host_compared_exactly(self):
        body = func("auth_header")
        assert '"$host" = "$api_host"' in body
        assert '*"$API_HOST"*' not in body

    def _run(self, url: str) -> str:
        script = f'''
API_HOST="https://api.cnb.cool"
CNB_TOKEN="secret-token"
''' + func("auth_header") + f'''
auth_header "{url}"
'''
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20).stdout

    def test_api_host_gets_header(self):
        assert self._run("https://api.cnb.cool/broccoli_xi/x/-/git/raw/main/a.py").startswith("Authorization: Basic")

    def test_lookalike_urls_get_nothing(self):
        for url in (
            "https://evil.example/?x=api.cnb.cool",
            "https://api.cnb.cool.attacker.example/x",
            "https://notapi.cnb.cool/x",
            "http://cnb.cool/api.cnb.cool",   # 路径里出现，主机名不对
        ):
            assert self._run(url).strip() == "", f"不该把令牌发给 {url}"

    def test_web_host_gets_nothing(self):
        assert self._run("https://cnb.cool/a/b/-/git/archive/main.tar.gz").strip() == ""


class TestExplicitRuntimeIsHonoured:
    """`--runtime docker` 在没有 Docker 的机器上不能悄悄改成 Python。

    两种方式的数据、日志、启停方法全不一样。换掉之后 `docker ps` 里找不到容器，
    看着像没装成功 —— 而屏幕上只留一句「改用 Python」。
    """

    def test_explicit_docker_dies(self):
        body = BODY[BODY.index('case "$want" in'):]
        body = body[: body.index("\n  esac")]
        assert "RUNTIME_OVERRIDE" in body
        assert "die" in body, "显式指定却没 Docker，应当停下报错"

    def test_explicit_docker_without_docker_fails(self, tmp_path):
        script = f'''
VERSION=test
RUNTIME_OVERRIDE="docker"
prev_runtime=""
die() {{ printf "DIE:%s\\n" "$*"; exit 1; }}
warn() {{ printf "! %s\\n" "$*"; }}
have_docker() {{ return 1; }}
have_python() {{ return 0; }}
want="$RUNTIME_OVERRIDE"
''' + BODY[BODY.index('  case "$want" in'): BODY.index('case "$want" in') + BODY[BODY.index('case "$want" in'):].index("\n  esac") + 7] + '''
echo "WANT=$want"
'''
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)
        assert "DIE" in r.stdout, "显式 --runtime docker 且 docker 不可用，必须停下"


class TestPublicUrlPort:
    """对外地址里的端口不能被拼成两个。

    以前判据是「不含 :$PORT 就补一个」，于是 `s.example.com:8080` + 端口 18960
    成了 `s.example.com:8080:18960`，而 `http://s.example.com:8080` 也不认。
    两种都只在屏幕上错一行，装完拿去发短链才发现链是坏的。
    """

    def _build(self, answer: str, port: str) -> str:
        start = BODY.index('      # 端口只在「人没自己写」时才补')
        end = BODY.index('      ok "对外地址：$PUBLIC_URL"')
        seg = BODY[start:end]
        script = (
            'PUBLIC_URL=""\n'
            f'PORT="{port}"\n'
            'warn() { printf "! %s\\n" "$*"; }\n'
            f'case "{answer}" in\n'
            f'  http://*|https://*) PUBLIC_URL="{answer}" ;;\n'
            f'  *) PUBLIC_URL="http://{answer}" ;;\n'
            'esac\n'
            + seg
            + '\nprintf "%s" "$PUBLIC_URL"\n'
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20).stdout

    def test_host_with_port_is_kept(self):
        assert self._build("s.example.com:8080", "18960") == "http://s.example.com:8080"

    def test_url_with_scheme_and_port_is_kept(self):
        assert self._build("http://s.example.com:8080", "18960") == "http://s.example.com:8080"

    def test_host_without_port_gets_listen_port(self):
        assert self._build("s.example.com", "18960") == "http://s.example.com:18960"

    def test_https_gets_no_extra_port(self):
        assert self._build("https://s.example.com", "18960") == "https://s.example.com"


class TestDoctorAgreesWithInstaller:
    """体检的结论必须和安装时的实际行为一致。

    `--port 18972` 且 18972 被别人占着：doctor 说「装的时候会自动改用 18973」，
    转身 check_port_free 说「仍按你指定的 18972 继续，起不来就报错」。
    同一件事两个说法，比只说一次错的还糟。
    """

    def test_explicit_port_not_promised_a_fallback(self):
        start = BODY.index('d_warn "端口 $PORT 被别的程序占用')
        seg = BODY[start: start + 700]
        assert "PORT_EXPLICIT" in seg, "体检要按「端口是不是显式给的」分开说话"

    def test_recorded_port_is_not_silently_changed(self):
        """沿用安装记录里的端口时，也不该「自动换个口」。"""
        body = func("check_port_free")
        assert "PORT_FROM_MARK" in body
        tail = BODY[BODY.index("check_port_free()"):]
        assert "PORT_FROM_MARK=\"y\"" in tail, "旗标要在 do_install 里置上，否则分支永远不走"


class TestNonInstallNameDoesNotHijack:
    """把脚本另存成别的名字、在一个空目录里跑 —— 不该去改机器上已有的那份站点。

    实测：`bash /tmp/suB/install.sh`（目录里只有这份拷贝）问的是「站点名称 /
    端口」，装的却是 /root/simpleurl，还把人已经配好的站点换了端口。
    现在只要文件名不带 install，就落到脚本自己所在的目录。
    """

    def test_name_based_guard_present(self):
        head = BODY[: BODY.index('PORT="${PORT:-8940}"')]
        assert "_self_name" in head
        assert "*install*" in head

    def test_arbitrary_name_resolves_to_own_dir(self, tmp_path):
        d = tmp_path / "copy"
        d.mkdir()
        f = d / "mysetup.sh"
        f.write_text(BODY)
        head = BODY[: BODY.index('PORT="${PORT:-8940}"')]
        r = subprocess.run(
            ["bash", "-c", head + '\nprintf "DIR=%s\\n" "$INSTALL_DIR"'],
            capture_output=True, text=True, timeout=30,
            env={**__import__("os").environ, "HOME": str(tmp_path), "BASH_SOURCE": str(f)},
        )
        assert f"DIR={d}" in r.stdout, f"另存的脚本应当管自己所在的目录，实际：{r.stdout} {r.stderr}"


class TestScriptVersion:
    def test_version_bumped(self):
        assert 'VERSION="2.7.0"' in BODY

    def test_syntax_ok(self):
        r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_cli_template_syntax_ok(self):
        r = subprocess.run(["bash", "-n"], input=cli_template(), capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
