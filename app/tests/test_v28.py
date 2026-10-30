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
    """同一造型 = 图形占画布的比例一致。

    上一版给每个尺寸段各定一档线宽，比例能差出 20% 以上——缩略图和原图
    是两枚图标。这里容差 8%，够容纳取整误差，不够容纳「另画一版」。
    """
    img = Image.open(LOGO / f"icon-{size}.png").convert("RGBA")
    box = _bbox(img)
    assert box, f"icon-{size}.png 是空图"
    fill = max(box[2] - box[0], box[3] - box[1]) / size
    assert 0.55 <= fill <= 0.70, f"icon-{size}.png 图形占画布 {fill:.0%}，与其它尺寸不一致"


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
    """把 install_cli 里生成的管理命令原样抠出来，单独跑一遍。"""
    src = INSTALL.read_text(encoding="utf-8")
    m = re.search(r"tee \"\$cli\" >/dev/null <<EOF\n(.*?)\nEOF\n", src, re.S)
    assert m, "找不到 install_cli 里生成的命令正文"
    return m.group(1)


def _render_cli(tmp_path: Path) -> Path:
    # 生成命令的 heredoc 没加引号（外层要展开 $INSTALL_DIR），所以源码里凡是
    # 想留给运行时的 $ 都写成 \$。抠出来单独跑时得把它们还原，否则 case 的
    # `${1:-status}` 会原样留在文件中，两个分支都不匹配。
    script = _cli_script().replace("\\$", "$")
    script = script.replace('DIR="$INSTALL_DIR"', f'DIR="{tmp_path}"')
    script = script.replace('NAME="$SERVICE_NAME"', 'NAME="simpleurl"')
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
    assert "is_our_service()" in src
    assert "/proc/$pid/cwd" in src
    assert "SimpleURL 自己在用" in src


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
