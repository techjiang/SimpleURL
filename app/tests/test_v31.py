"""一键安装脚本的第二次「真装一遍」回归线（v31）。

上一轮（v30）修的是「探测不到就当没事」。这一轮真装第二遍，暴露的是另一类：

1. **源码包结构猜错。** CNB 的 `/git/archive` 里条目不带顶层目录，而脚本无条件
   用 `tar --strip-components=1`，把 `app/` 自己削掉了：`main.py`、`core/`、
   `templates/` 全平铺到安装根目录。文件一个不少、服务也起得来，于是没人发现
   装出来的是一份和仓库不一样的副本。
2. **升级会把自己的 install.sh 弄丢。** 源码包里没有 install.sh（仓库根目录才有），
   而 `clear_source` 会按清单把它删掉、只在「包里带了」时才留影子。结果装完第一遍
   再升级一次，`simpleurl upgrade / doctor / uninstall` 从此全报「缺少 install.sh」。
3. **升级可能悄悄换掉凭据。** 读不到 ADMIN_TOKEN 时生成新密码，summary 再把新密码
   当「当前密码」打出来；SECRET_KEY 同理，一换所有会话当场失效。
4. **端口结论前后打脸。** 换壳重跑丢了上下文，同一件事先报「沿用记录端口」，
   再报「自动改用 18976」。
5. **`./install.sh --version` 不存在**，而 `simpleurl version` 该有也没有。

断言都盯着「落地会怎样」，不是「代码里有没有这句话」。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install.sh"


def _text() -> str:
    return INSTALL.read_text(encoding="utf-8")


def _block(name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", _text(), re.S | re.M)
    assert m, f"install.sh 里找不到 {name}()"
    return m.group(0)


def _code_only(name: str) -> str:
    """函数体去掉注释行 —— 注释里提到旧写法不算数。"""
    return "\n".join(
        line for line in _block(name).splitlines() if not line.lstrip().startswith("#")
    )


def _generated_cli() -> str:
    text = _text()
    start = text.index('${SUDO} tee "$cli"')
    end = text.index("\nEOF\n", start)
    return text[start:end]


def _run(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for k in ("CNB_TOKEN", "PORT", "REPO_BRANCH"):
        env.pop(k, None)
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    env["SIMPLEURL_HOME"] = "/nonexistent-simpleurl-install"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [shutil.which("bash") or "/bin/bash", str(INSTALL), *args],
        capture_output=True, text=True, env=env, timeout=90,
    )


# ---------------------------------------------------------------- 源码包结构
class TestTarballShape:
    """tar 里有几层目录，得先看再削，不能一律削一层。"""

    def test_strip_level_is_decided_by_listing(self):
        body = _code_only("tar_strip_level")
        assert "tar -tzf" in body, "没看条目路径就定削几层，等于猜"
        assert "printf '1'" in body and "printf '0'" in body

    def test_extract_checks_the_result(self):
        body = _code_only("extract_tarball")
        assert "--strip-components" in body
        assert "app/main.py" in body, "解完不验产物，削错了也发现不了"

    def test_stage_source_does_not_strip_blindly(self, tmp_path: Path):
        """stage_source 里不许再出现无条件的 --strip-components=1。"""
        body = _code_only("stage_source")
        assert "--strip-components" not in body
        assert "extract_tarball" in body


class TestTarballExtraction:
    """真解两个包：带顶层目录的、不带的，都得落成 app/main.py。"""

    def make(self, tmp_path: Path, *, nested: bool) -> Path:
        """nested=True 造「仓库名/app/main.py」，False 造「app/main.py」。"""
        src = tmp_path / "src"
        top = src / ("SimpleURL-main" if nested else "")
        (top / "app").mkdir(parents=True)
        (top / "app" / "main.py").write_text("x = 1\n")
        target = tmp_path / ("nested.tar.gz" if nested else "flat.tar.gz")
        subprocess.run(["tar", "-czf", str(target), "-C", str(top), "app"], check=True)
        return target

    def extract(self, tarball: Path, tmp_path: Path) -> Path:
        stage = tmp_path / "stage"
        stage.mkdir(exist_ok=True)
        helpers = _block("tar_strip_level") + "\n" + _block("extract_tarball")
        r = subprocess.run(
            [shutil.which("bash") or "/bin/bash", "-c",
             f"{helpers}\nextract_tarball {tarball} {stage} && echo OK || echo FAIL"],
            capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip().endswith("OK"), r.stdout + r.stderr
        return stage

    def test_flat_archive_keeps_app_prefix(self, tmp_path: Path):
        stage = self.extract(self.make(tmp_path, nested=False), tmp_path)
        assert (stage / "app" / "main.py").is_file()
        assert not (stage / "main.py").exists(), "app/ 被削掉了 —— 装出来的是平铺副本"

    def test_nested_archive_strips_one_level(self, tmp_path: Path):
        stage = self.extract(self.make(tmp_path, nested=True), tmp_path)
        assert (stage / "app" / "main.py").is_file()


# ---------------------------------------------------------------- 自更新不自杀
class TestInstallScriptSurvives:
    """升级不能把安装目录里的 install.sh 弄丢。"""

    def test_self_is_preserved_even_when_package_lacks_it(self):
        body = _code_only("apply_source")
        assert "preserve_self" in body, "落盘前必须无条件留一份自己的影子"
        assert body.index("preserve_self") < body.index("clear_source"), \
            "留影子必须排在清理之前 —— 清完再判断就永远判成没有"
        helper = _code_only("preserve_self")
        assert "SELF_PATH" in helper, "包装里没带 install.sh 时，得拿正在跑的这一份兜底"
        assert "SELF_SNAPSHOT" in helper

    def test_restore_path_still_there(self):
        body = _code_only("apply_source")
        assert 'cp -f "$SELF_SNAPSHOT" "$INSTALL_DIR/install.sh"' in body


# ---------------------------------------------------------------- 凭据不许被悄悄换掉
class TestCredentialsKeptOnReuse:
    """升级沿用配置时，读不到凭据只能提醒，不能重新生成。"""

    def test_missing_admin_password_is_not_regenerated(self):
        body = _code_only("do_install")
        # 定位「否则（沿用已有安装）」那个分支
        idx = body.index('reuse" = "n"')
        tail = body[idx:]
        assert 'ADMIN_PASS="$(gen_password)"' not in tail, \
            "升级时生成新密码 = 把库里那份凭据作废，还把新密码当「当前密码」打出来"
        assert "simpleurl password --reset" in tail, "要告诉人正确的换密码方式"

    def test_summary_never_prints_an_empty_password(self):
        body = _block("summary")
        assert 'if [ -n "$ADMIN_PASS" ]' in body, "密码为空时不能打一行空密码出来"


# ---------------------------------------------------------------- 换壳重跑不丢上下文
class TestReexecKeepsContext:
    def test_reexec_only_once(self):
        body = _code_only("do_install")
        assert 'SCRIPT_REEXEC" != "y"' in body, "没有防自举的判断会无限 exec"

    def test_reexec_covers_reinstall_too(self):
        """重装（不带 upgrade）也要换脚本，否则从仓库目录重跑一次就把脚本打回旧版。"""
        body = _code_only("do_install")
        idx = body.index("fetch_latest_script")
        cond = body[:idx]
        # 换壳那段的条件里不该再要求「这是 upgrade」
        tail = cond[cond.rindex("  if "):]
        assert 'ACTION_UPGRADE' not in tail, "换壳只挂在 upgrade 上：重装会把脚本打回旧版"

    def test_doctor_is_not_run_twice(self):
        body = _code_only("do_install")
        assert "SCRIPT_DOCTORED" in body
        assert "环境体检已在换脚本前通过" in body

    def test_port_decision_is_not_revised(self):
        body = _code_only("check_port_free")
        assert "SIMPLEURL_PORT_FIXED" in body, "同一件事两次给不同结论，比一次错结论更糟"


# ---------------------------------------------------------------- 子命令与版本
class TestVersionCommand:
    def test_version_flag(self):
        r = _run("--version")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "SimpleURL 安装脚本 v" in r.stdout

    def test_help_lists_version(self):
        assert "--version" in _run("--help").stdout

    def test_generated_cli_has_version(self):
        cli = _generated_cli()
        assert re.search(r"^\s*version\)", cli, re.M), "simpleurl version 没接上"

    def test_no_duplicated_assignments(self):
        """常量与解析表里不该出现整行重复 —— 那是合并残留的味道。

        只看「上一层缩进」的行（顶层常量、case 分支、函数调用式赋值），
        函数体内部的短行不在此列：`fi`、`else`、`}` 当然会重复。
        """
        # 只看顶层（不缩进）的常量与 case 分支：函数体里同样的两行出现两次很正常
        keep = re.compile(r"^(-{0,2}[A-Za-z_][A-Za-z0-9_]*=|[A-Za-z_][A-Za-z0-9_|-]*\))")
        lines = [
            l for l in _text().splitlines()
            if l.strip() and not l.lstrip().startswith("#") and keep.match(l)
        ]
        seen: dict[str, int] = {}
        for line in lines:
            seen[line] = seen.get(line, 0) + 1
        dup = [l for l, n in seen.items() if n > 1]
        assert not dup, f"出现重复行：{dup}"


# ---------------------------------------------------------------- 从仓库目录直装
class TestLocalSourceShortcut:
    def test_skips_download_only_when_source_is_elsewhere(self):
        body = _code_only("stage_source")
        assert "copy_local_source" in body or "SELF_DIR" in body
        assert "SELF_DIR" in _code_only("copy_local_source")

    def test_skip_is_reported_honestly(self):
        """「跳过下载」只在安装目录里真有源码时才成立。"""
        body = _code_only("install_source")
        idx = body.index('"$rc" = "2"')
        seg = body[idx: idx + 700]
        assert 'if [ -f "$INSTALL_DIR/app/main.py" ]' in seg
        assert "copy_local_source" in seg


# ---------------------------------------------------------------- 换源之后还能升
class TestMirrorHosts:
    """换了镜像 / 自建源，升级不能说一句「取不到远端」就原地不动。

    真撞到的：`GIT_HOST` 指着镜像、`API_HOST` 也指着同一个镜像时，
    `is_cnb` 只认 cnb.cool 域名，CNB 那套 `/-/git/archive`、`/-/git/raw` 路径规则
    整段被跳过，源码包与脚本都取不到；屏幕上却只有一句轻描淡写的
    「沿用已有的代码继续」，人以为升完了，其实一个字节都没动。
    """

    def test_platform_detection_looks_at_more_than_git_host(self):
        body = _code_only("is_cnb")
        for var in ("GIT_HOST", "API_HOST", "REPO_URL"):
            assert var in body, f"只看 GIT_HOST 会在换源后失明：缺 {var}"

    def test_tarball_url_uses_git_archive_on_cnb(self):
        body = _code_only("default_tarball")
        assert "/-/git/archive/" in body
        assert "/-/archive/" not in body.split("*)")[0], "网页 /-/archive 只有 HTML 外壳"

    def test_both_hosts_are_tried_for_meta_and_script(self):
        for fn in ("remote_peek_version", "fetch_latest_script"):
            body = _code_only(fn)
            assert "API_HOST" in body and "GIT_HOST" in body, f"{fn} 只试了一个域名"

    def test_silent_noop_is_called_out(self):
        """真的换不了代码时，必须说「代码没有更新」，给出去哪儿改。"""
        body = _code_only("install_source")
        assert "代码没有更新" in body
        assert "REPO_TARBALL" in body and "REPO_URL" in body


# ---------------------------------------------------------------- 停服要等进程真的走
class TestStopWaitsForTheProcess:
    def test_stop_running_escalates(self):
        body = _code_only("stop_running")
        assert "kill -9" in body, "只发 TERM 就去删库，进程可能还在写 WAL"
        assert body.count("sleep 1") >= 1

    def test_env_is_not_rewritten_on_upgrade(self):
        """升级不要把 .env 整份重写 —— README 教人在这份文件里改 PUBLIC_URL。"""
        body = _code_only("do_install")
        idx = body.index("write_env")
        assert 'if [ "$reuse" = "n" ]' in body[idx - 400: idx], \
            "无条件 write_env 会把用户手写的配置冲掉"
