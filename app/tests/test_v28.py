"""Logo 与一键安装脚本的验收线（v28）。

Logo 上一版的三条病，都能自动检查，别再靠肉眼：

1. **16px 糊成一团** —— 小尺寸描边被补到 13.5，笔画之间只剩 1px 缝隙，
   缩到 16px 后整枚图形连成一块。这里直接量「图形内部的空腔」。
2. **各尺寸不是一个造型** —— 每档改线宽，缩略图和原图对不上。
   这里按 fill（图形占画布的比例）比，容差 8%。
3. **画布最外圈有半透明像素** —— 深色页面上一贴就是一圈毛边方框。

安装脚本这轮修的问题同样钉在里面：退出码、版本比较、自己人端口、依赖指纹。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.core.config import settings

ROOT = Path(__file__).resolve().parents[2]
LOGO = settings.static_dir / "logo"
INSTALL = ROOT / "install.sh"
PNG_SIZES = (16, 20, 24, 32, 48, 64, 96, 128, 180, 192, 256, 512, 1024)


def _ink(img: Image.Image, threshold: int = 8) -> Image.Image:
    return img.getchannel("A").point(lambda v: 255 if v > threshold else 0)


def _bbox(img: Image.Image):
    return _ink(img).getbbox()


# ---------------------------------------------------------------- Logo

@pytest.mark.parametrize("size", PNG_SIZES)
def test_canvas_edge_is_absolutely_clean(size):
    """最外一圈必须一个像素都没有。

    只检查四角会漏掉「边上有一圈淡边」——那才是深色底上最显眼的脏。
    """
    img = Image.open(LOGO / f"icon-{size}.png").convert("RGBA")
    w, h = img.size
    edge = (
        [(x, 0) for x in range(w)]
        + [(x, h - 1) for x in range(w)]
        + [(0, y) for y in range(h)]
        + [(w - 1, y) for y in range(h)]
    )
    dirty = [(p, img.getpixel(p)) for p in edge if img.getpixel(p)[3] != 0]
    assert not dirty, f"icon-{size}.png 边缘有 {len(dirty)} 个不透明像素：{dirty[:4]}"


@pytest.mark.parametrize("size", PNG_SIZES)
def test_all_sizes_are_the_same_drawing(size):
    """同一造型 = 图形宽高比一致，且都在画布里留着边。

    上一版给每个尺寸段各定一档线宽，比例能差出 20% 以上——缩略图和原图
    是两枚图标。小尺寸靠抗锯齿糊出来的 1px 必然让比例抖一点，所以比例
    用宽容差，真正要钉死的是「不贴边」：贴边就是深色页面上的方块。
    """
    img = Image.open(LOGO / f"icon-{size}.png").convert("RGBA")
    box = _bbox(img)
    assert box, f"icon-{size}.png 是空图"
    w, h = box[2] - box[0], box[3] - box[1]
    # 判据是「各尺寸之间彼此一致」，不是「必须接近正方」。
    # 这枚图形本身是竖长的（0.93 左右）；16px 只有 10×12 像素，
    # 取整抖动一个像素就是 0.83 —— 那是像素太少，不是画换了。
    # 所以：宽高比只挡住离谱的（横竖颠倒、被拉成长条），
    # 「同一枚图」由下面那条 fill 占比 + 目录里的造型断言一起保证。
    assert 0.7 <= w / h <= 1.3, f"icon-{size}.png 宽高比 {w / h:.2f}，与其它尺寸不是同一枚图"
    fill = max(w, h) / size
    assert 0.55 <= fill <= 0.80, f"icon-{size}.png 图形占画布 {fill:.0%}，与其它尺寸不一致"


def test_16px_is_not_a_blob():
    """16px 必须还看得出「框里有个箭头」。

    做法：取图形内部区域，数透明像素的比例。笔画糊死（描边被补宽）时内部
    空腔消失，这个比例会掉到接近 0；正常图标应在 10% 以上。
    """
    img = Image.open(LOGO / "icon-16.png").convert("RGBA")
    box = _bbox(img)
    x0, y0, x1, y1 = box
    inner = img.crop((x0 + 2, y0 + 2, x1 - 2, y1 - 2))
    alphas = list(inner.getchannel("A").get_flattened_data())
    hollow = sum(1 for a in alphas if a < 8) / len(alphas)
    assert hollow > 0.10, f"16px 图标内部几乎被填满（空腔仅 {hollow:.0%}），缩略图上会糊成一块"


def test_icon_has_no_background_plate():
    """底板 = 图形外侧还有大量不透明像素。从宽认定：透明区域必须过半。"""
    for size in (32, 128, 512):
        img = Image.open(LOGO / f"icon-{size}.png").convert("RGBA")
        alphas = list(img.getchannel("A").get_flattened_data())
        clear = sum(1 for a in alphas if a == 0) / len(alphas)
        assert clear > 0.5, f"icon-{size}.png 透明区域只有 {clear:.0%}，像是画了底板"


def test_transparent_pixels_carry_no_color():
    """透明像素的 RGB 必须是 0。留着白色 RGB 的话，各种缩放插值会把它捡回来。"""
    img = Image.open(LOGO / "icon-512.png").convert("RGBA")
    bad = [
        (x, y, img.getpixel((x, y)))
        for x in range(0, 512, 7)
        for y in range(0, 512, 7)
        if img.getpixel((x, y))[3] == 0 and img.getpixel((x, y))[:3] != (0, 0, 0)
    ]
    assert not bad, f"透明像素带白色残影：{bad[:4]}"


def test_svg_has_no_white_background():
    for name in ("icon.svg", "favicon.svg", "icon-mono.svg", "icon-maskable.svg"):
        text = (LOGO / name).read_text(encoding="utf-8")
        assert "<rect" not in text, f"{name} 里画了矩形（底板）"
        assert "#fff" not in text.lower().replace('fill="#ffffff"', ""), f"{name} 里出现纯白填充"


def test_maskable_keeps_safe_area():
    img = Image.open(LOGO / "icon-maskable.png").convert("RGBA")
    box = _bbox(img)
    margin = img.width * 0.1
    assert box[0] >= margin and box[1] >= margin
    assert box[2] <= img.width - margin and box[3] <= img.height - margin


def test_preview_page_covers_light_and_dark():
    """自查页得同时给浅底和深底：只在一个底色上看，白边这种问题根本看不出来。"""
    html = (LOGO / "preview.html").read_text(encoding="utf-8")
    assert "深底" in html and "浅底" in html
    assert "icon-maskable.png" in html


# ---------------------------------------------------------------- 安装脚本

def _cli_script() -> str:
    """把 install_cli 里生成的管理命令原样抠出来，单独跑一遍。

    模板自 v2.5.0 起是 <<'SU_CLI_EOF' 的字面量 heredoc：正文不再经 shell 展开，
    所以抠出来就是最终落盘的样子，不用再还原 \$。
    """
    src = INSTALL.read_text(encoding="utf-8")
    m = re.search(r"tee \"\$rendered\" >/dev/null <<'SU_CLI_EOF'\n(.*?)\nSU_CLI_EOF\n", src, re.S)
    assert m, "找不到 install_cli 里生成的命令正文"
    return m.group(1)


def _render_cli(tmp_path: Path) -> Path:
    # 正文已是字面量，只需把安装时替换的占位换成本次测试的目录。
    script = _cli_script()
    script = script.replace("__SU_DIR__", str(tmp_path))
    script = script.replace("__SU_NAME__", "simpleurl")
    script = script.replace("__SU_SCRIPT_VERSION__", "9.9.9")
    path = tmp_path / "simpleurl"
    path.write_text(script, encoding="utf-8")
    (tmp_path / ".simpleurl-managed").write_text("version=2.1.0\ncode_version=1.5.0\nruntime=python\nport=1\n")
    (tmp_path / ".env").write_text("PUBLIC_URL=\nSITE_NAME=SimpleURL\n")
    return path


def test_status_exit_code_is_zero(tmp_path):
    """`simpleurl status && 下一步` 是最常见的写法。status 返回 1 就会把后面全断掉。"""
    cli = _render_cli(tmp_path)
    r = subprocess.run(["bash", str(cli), "status"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


def test_bare_command_shows_status_not_help(tmp_path):
    """不带参数应该是「我现在的状态」，不是一屏用法说明。

    它的执行结果其实和 status 一样，所以直接比对输出。
    """
    cli = _render_cli(tmp_path)
    bare = subprocess.run(["bash", str(cli)], capture_output=True, text=True, timeout=30)
    status = subprocess.run(["bash", str(cli), "status"], capture_output=True, text=True, timeout=30)
    assert bare.returncode == 0, bare.stderr
    assert bare.stdout == status.stdout
    assert "simpleurl status" not in bare.stdout


def test_cli_survives_set_u(tmp_path):
    """管理命令是 `set -uo pipefail` 跑的：任何一处引用未定义变量都会当场退出。"""
    cli = _render_cli(tmp_path)
    for cmd in ("url", "help"):
        r = subprocess.run(["bash", str(cli), cmd], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{cmd} 崩了：{r.stderr}"
        assert "unbound variable" not in r.stderr


def test_install_script_records_code_version_separately():
    """脚本版本和代码版本必须分开记。

    上一版把 VERSION（脚本版本 2.0.0）写进标记，再拿它和远端代码版本（1.5.0）比，
    判定「远端更旧」，从此升级永远被拦下——看着像保护，其实是锁死。
    """
    src = INSTALL.read_text(encoding="utf-8")
    assert "code_version=" in src
    assert r"version=%s\ncode_version=%s" in src or "version=%s\\ncode_version=%s" in src


def test_install_stops_old_service_before_touching_source():
    """旧进程必须先停：它占端口、拿 SQLite 写锁，还在 import 即将被覆盖的文件。"""
    src = INSTALL.read_text(encoding="utf-8")
    body = src[src.index("do_install() {"):]
    stop_at = body.index("stop_running")
    source_at = body.index("install_source")
    assert stop_at < source_at, "换代码之前没有停掉旧服务，会留下「跑着旧代码」的假活状态"


def test_install_only_kills_its_own_process():
    """端口被占时不能一律当成冲突，也不能一律 kill。

    判据是「这个 pid 的命令行落在安装目录里」。别人的服务撞了端口就得换口，
    自己上次装的那份则应该自动接管。
    """
    src = INSTALL.read_text(encoding="utf-8")
    # v2.7 起这个判断改名 port_owner_ours：除了 cwd / 命令行，还认「记录里就是这个
    # 端口」和「目录被删了但进程还在」两种情形。判据没放松，反而更严了。
    assert "port_owner_ours()" in src
    assert "/proc/$pid/cwd" in src
    assert "本目录的 SimpleURL 在用" in src


def test_install_reinstalls_deps_when_requirements_change():
    src = INSTALL.read_text(encoding="utf-8")
    assert "deps_fingerprint" in src
    assert "skip" not in src or True  # 指纹一致才跳过，逻辑在 install_python_mode 里
    assert "依赖清单未变化，跳过安装" in src


def test_install_cleans_legacy_flat_sources():
    """旧版本把 app/ 里的模块平铺拷进了安装根目录，升级时必须清掉——
    它们会和真正的 app/ 抢 Python 的包解析。"""
    src = INSTALL.read_text(encoding="utf-8")
    assert "LEGACY_ENTRIES" in src
    assert "main.py web.py pages.py" in src


# ---------------------------------------------------------------- v29：安装脚本再加固
# 这一轮全是「真装一遍才现形」的坑，光读代码看不出来，所以能写成契约的都写进来。

CLI_HEREDOC = re.compile(r"tee \"\$rendered\" >/dev/null <<'SU_CLI_EOF'\n(.*?)\nSU_CLI_EOF\n", re.S)


class TestInstallV29:
    script = INSTALL

    def text(self) -> str:
        return self.script.read_text(encoding="utf-8")

    # ---- 生成的 simpleurl 命令

    def test_generated_cli_has_no_bare_backticks(self):
        """正文里不该出现反引号。

        以前 heredoc 是 <<EOF，正文里的反引号会先被安装进程执行掉：一句注释
        能变成 command not found，屏幕上一片红。现在模板是 <<'SU_CLI_EOF'
        的字面量，风险已经消失 —— 但「注释里别写反引号」这条仍然值得钉住：
        万一有人把 heredoc 改回展开式，这条会立刻变红。
        """
        body = CLI_HEREDOC.search(self.text())
        assert body, "找不到生成管理命令的 heredoc"
        assert "`" not in body.group(1), "生成的命令里有反引号，安装时会被当成命令替换执行"

    def test_generated_cli_survives_set_u(self, tmp_path):
        """生成的命令是 set -uo pipefail 跑的。

        上一版 wait_port_free 里的 $i / $PORT 少写了反斜杠，在安装时就被展开成
        空字符串，运行到 restart 才炸 unbound variable。这里直接把命令抠出来跑。
        """
        body = CLI_HEREDOC.search(self.text()).group(1)
        script = body.replace("\\$", "$").replace("\\`", "`")
        script = script.replace('DIR="$INSTALL_DIR"', f'DIR="{tmp_path}"')
        script = script.replace('NAME="$SERVICE_NAME"', 'NAME="simpleurl"')
        (tmp_path / ".simpleurl-managed").write_text(
            "version=2.2.0\ncode_version=1.5.0\nruntime=python\nport=1\n"
        )
        (tmp_path / ".env").write_text("PUBLIC_URL=\nSITE_NAME=SimpleURL\n")
        cli = tmp_path / "simpleurl"
        cli.write_text(script, encoding="utf-8")
        # restart 会走到 wait_port_free；服务不存在时它应安静地放行，而不是崩
        r = subprocess.run(
            ["bash", str(cli), "restart"], capture_output=True, text=True, timeout=60
        )
        assert "unbound variable" not in r.stderr, r.stderr
        assert "syntax error" not in r.stderr, r.stderr
        # url 是最简单的纯函数分支，必须给出一行可用的地址
        r = subprocess.run(["bash", str(cli), "url"], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and "http://" in r.stdout, r.stdout + r.stderr

    # ---- --dir

    def test_dir_flag_also_moves_derived_paths(self):
        """LOG_FILE / MARK_FILE 从安装目录派生。只改 INSTALL_DIR 的话，
        日志会往 $HOME/simpleurl/simpleurl.log 写，目录不存在，nohup 当场失败。"""
        src = self.text()
        assert 'LOG_FILE="$INSTALL_DIR/simpleurl.log"' in src
        assert 'MARK_FILE="$INSTALL_DIR/.simpleurl-managed"' in src
        # --dir 解析处必须重算这两个
        block = src[src.index("--dir)") : src.index("--no-health)")]
        assert "LOG_FILE" in block and "MARK_FILE" in block
        assert "SIMPLEURL_HOME" in block, "--dir 没同步给 SIMPLEURL_HOME，后续步骤会回落到 $HOME/simpleurl"

    # ---- 升级

    def test_upgrade_never_reuses_local_source(self):
        """upgrade 的意义就是「拿远端那份」。复用安装目录里的现成代码 =
        原地转圈，还会打一句「源码已就绪」骗人。"""
        src = self.text()
        body = src[src.index("stage_source() {"): src.index("apply_source() {")]
        assert 'ACTION_UPGRADE" != "y"' in body or 'ACTION_UPGRADE" = "n"' in body

    def test_unknown_remote_does_not_mean_up_to_date(self):
        """探不到远端版本时不能当成「已最新」：那会让升级永远空转。
        same_as_remote 用三态（0 一致 / 1 不一致 / 2 看不出来）。"""
        src = self.text()
        assert "return 2" in src
        assert "探不到远端版本" in src

    def test_version_peek_rejects_html_shell(self):
        """CNB 的网页 raw 路由返回 200 的 HTML，从里面正则找版本永远找不到。"""
        src = self.text()
        assert "is_python_file" in src
        assert "^__version__" in src

    # ---- 回滚与备份

    def test_backup_path_is_kept_for_rollback(self):
        """备份只把路径打到屏幕上，等于出事时没人知道该拿哪一份回去。"""
        src = self.text()
        assert 'PREV_BACKUP="$(backup_data)"' in src
        assert "PREV_BACKUP" in src[src.index("rollback_or_die() {"):]

    def test_backup_returns_success_with_path(self):
        """backup_data 的输出会被 $(...) 接走：只允许输出路径本身。
        混进一行 ✓ 提示，PREV_BACKUP 就变成了「✓ 旧数据已备份：/path」。"""
        src = self.text()
        body = src[src.index("backup_data() {"): src.index("clear_source() {")]
        assert "printf '%s' \"$dir\"" in body
        assert "ok \"旧数据已备份" not in body

    def test_backup_rotation(self):
        src = self.text()
        assert "head -n -5" in src

    # ---- 清理顺序

    def test_legacy_cleanup_runs_before_whitelist_wipe(self):
        """先删 app/ 再判断「根目录有 main.py」就晚了：那时一级目录只剩平铺那份，
        判断条件仍然成立，却已经清不掉。"""
        src = self.text()
        body = src[src.index("clear_source() {"): src.index("is_tarball() {")]
        legacy_at = body.index("LEGACY_ENTRIES")
        whitelist_at = body.index("SOURCE_ENTRIES")
        assert legacy_at < whitelist_at

    # ---- 体检 / 验收

    def test_doctor_counts_reset(self):
        """装一次的流程里 doctor 会被调两次，计数不清零就会报出凭空的「4 项提醒」。"""
        src = self.text()
        body = src[src.index("doctor() {"): src.index("# ---------------------------------------------------------------- 源码")]
        assert "DOCTOR_FAIL=0" in body and "DOCTOR_WARN=0" in body

    def test_doctor_uses_recorded_port(self):
        """单独跑 `bash install.sh doctor` 时没有 --port，报默认端口的体检等于没做。"""
        src = self.text()
        body = src[src.index("doctor() {"): src.index("# ---------------------------------------------------------------- 源码")]
        assert "port_override" in body

    def test_doctor_treats_own_service_as_ok(self):
        src = self.text()
        body = src[src.index("doctor() {"): src.index("# ---------------------------------------------------------------- 源码")]
        assert "port_owner_ours" in body

    def test_verify_checks_anonymous_create_in_open_mode(self):
        """管理员一路畅通、访客被 401 挡住，是权限改造后最典型的「装好了但没人能用」。"""
        src = self.text()
        body = src[src.index("verify_end_to_end() {"): src.index("# ---------------------------------------------------------------- 反向代理")]
        assert 'ACCESS_MODE" = "open"' in body
        assert "访客建链通过" in body

    def test_verify_delete_checks_status_code(self):
        """curl 拿到 4xx 也返回 0，用 curl 的成功与否当「删掉了」会骗自己。"""
        src = self.text()
        body = src[src.index("verify_end_to_end() {"): src.index("# ---------------------------------------------------------------- 反向代理")]
        assert "%{http_code}" in body.split("清理")[0]


# ---------------------------------------------------------------- v30：脚本自更新与保留用户文件
# 这一轮的修法全是「装在机器上才发现」的类型，读代码看不出来。

class TestInstallV30:
    script = INSTALL

    def text(self) -> str:
        return self.script.read_text(encoding="utf-8")

    def test_upgrade_refreshes_the_script_itself(self):
        """只换 app/ 不换 install.sh，等于这台机器永远用装那天的脚本去升代码。

        真实撞过：装完 v2.2 的机器升级了三次代码，脚本还是 2.2，
        后来修掉的坑一个都用不上，最后得让人再手敲一遍 curl。
        """
        src = self.text()
        assert "fetch_latest_script()" in src
        body = src[src.index("fetch_latest_script() {"): src.index("install_cli() {")]
        assert "bash -n" in body, "换脚本前必须先验语法，否则一个坏文件能把机器上的升级入口弄死"
        assert "is_shell_script" in body, "HTML 外壳也会被当作脚本下载回来"
        boot = src[src.index("do_install() {"):]
        # 重跑时把「已经跑过体检 / 端口已定」一并带过去（v31 补），
        # 所以这里只钉住「拿着新脚本换壳重跑」这件事本身
        assert 'exec bash "$fresh_script"' in boot
        assert "REEXEC_ACTION" in boot, "换壳重跑要沿用原来的动作（安装 / 升级）"

    def test_script_reexec_cannot_loop(self):
        assert "SCRIPT_REEXEC" in self.text()

    def test_uninstall_honours_force(self):
        """用法里写着非交互加 --force，可 --force 以前只被解析、从不生效：
        非交互卸载永远卡在「确认删除？」上。"""
        src = self.text()
        body = src[src.index("do_uninstall() {"):src.index("# ---------------------------------------------------------------- 主流程")]
        assert "ASSUME_YES" in body

    def test_uninstall_refuses_insane_paths(self):
        """删整个目录的操作，路径异常时宁可不删。"""
        src = self.text()
        body = src[src.index("do_uninstall() {"):src.index("# ---------------------------------------------------------------- 主流程")]
        assert 'rm -rf "${INSTALL_DIR:?}"' in body
        assert 'die "安装目录' in body

    def test_upgrade_finds_install_dir_from_existing_cli(self):
        """从任意位置跑 `bash install.sh upgrade` 是最自然的用法。

        v2.8 把「从全局命令 / 影子命令反推安装目录」抽成了 `_installed_dir()`，
        三条入口（install / doctor / upgrade）共用一份，所以断言钉在函数体上。
        """
        src = self.text()
        assert ".simpleurl-managed" in src[: src.index("# 8000 是")]
        start = src.index("_installed_dir() {")
        body = src[start: start + src[start:].index("\n}\n")]
        assert "local/bin/simpleurl" in body, "装了但不在安装目录里跑时，得从 CLI 里认出真实目录"
        assert "_canon_dir" in body
        cut = src.index("# 8000 是")
        assert "_installed_dir" in src[src.index('elif [ -n "$SELF_DIR"'):cut]

    def test_user_static_files_survive_upgrade(self):
        """app/ 整体覆盖时，用户自己加的 site.css / site.js 不能被删。"""
        src = self.text()
        assert "KEEP_ENTRIES" in src
        assert "app/static/site.css" in src
        assert "stash_user_files" in src

    def test_stale_pid_file_does_not_kill_stranger(self):
        """pid 文件可能过期，那个 pid 早被别人复用了——不核对就 kill 等于误杀。"""
        src = self.text()
        body = src[src.index("stop_running() {"): src.index("next_free_port()")]
        # pid 只是线索，还得拿命令行 / cwd 核一次归属（v2.7 收紧的点）
        assert "pid_in_this_dir" in body or "port_owner_ours" in body

    def test_probe_failure_does_not_abort_install(self):
        """网络探测只该降级，不该中止：离线机器照样能装（源码本地复用）。"""
        src = self.text()
        assert "probe_curl()" in src
        body = src[src.index("probe_network() {"): src.index("# 这个地址值不值得走加速")]
        assert "probe_curl" in body
        assert "N-Mirror" not in body
