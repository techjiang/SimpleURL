"""子命令一致性与 Logo 造型的验收线（v29）。

这一轮两件事：

1. **帮助里写的子命令，照着敲必须能用。** `./install.sh status` 以前会落到
   「未知参数」上 —— 帮助里列得清清楚楚，照着敲却全报错。
2. **Logo 必须还是用户点名的那一枚。** 这是本轮最大的教训：之前三版「重做」
   都拿技术指标（透明、尺寸一致、不贴边、16px 空腔）当验收，全绿，图形却
   早被换成了我自己发明的造型。规格是用户指的那张图，不是我的测试。
   所以断言分两层：
   - **造型层**：两条路径的端点与半径、三档描边宽度、三个色值 —— 换了就挂
   - **像不像**：512px 缩到 96×96，跟「按原设计几何重绘」的参考图比 alpha，
     差异超过 3% 就挂（不逐像素比，那样一换渲染器就假警报）
"""


from __future__ import annotations

import io
import os
import re
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.core.config import settings

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install.sh"
LOGO = settings.static_dir / "logo"

SUBCOMMANDS = (
    "status", "start", "stop", "restart", "log", "url", "password",
    "config", "backup", "restore",
)


class TestSubcommandParity:
    script = INSTALL

    def run(self, *args):
        # PATH 里去掉已装的 simpleurl：这条测的是「这台机器还没装」时脚本自己的
        # 反应。不隔离的话，跑测试的机器上那份真命令会被转发出去 (restore 要参数、
        # log 会一直挂着)，测的就不是脚本了。
        env = dict(os.environ)
        env.pop("CNB_TOKEN", None)
        env["SIMPLEURL_HOME"] = str(ROOT / ".no-such-install")
        env["PATH"] = "/usr/bin:/bin"
        return subprocess.run(
            ["bash", str(self.script), *args],
            capture_output=True, text=True, env=env, timeout=60,
        )

    def test_help_lists_every_forwarded_command(self):
        """帮助与解析表必须来自同一份清单，靠这条挡「文档说有、代码没接」。"""
        out = self.run("--help").stdout
        for sub in SUBCOMMANDS:
            assert sub in out, f"帮助里没写 {sub}"

    @pytest.mark.parametrize("sub", SUBCOMMANDS)
    def test_subcommand_is_recognised(self, sub):
        """子命令不能被当成「未知参数」。

        没装过的机器上它会明说「还没装好」，但那是**认识**这个词之后的回答，
        不是一句「未知参数」。
        """
        r = self.run(sub)
        assert "未知参数" not in (r.stdout + r.stderr), f"{sub} 被当成未知参数了"
        if r.returncode != 0:
            assert "还没装好" in (r.stdout + r.stderr), r.stdout + r.stderr

    def test_unknown_flag_still_fails(self):
        """认识的子命令多了，不认识的东西照样得挡住，不然会默默跑成默认安装。"""
        r = self.run("--nope-nope")
        assert r.returncode != 0
        assert "未知参数" in (r.stdout + r.stderr)


class TestLogoIsTheOriginalArtwork:
    """钉住「用户点名的那一枚」：折返线 + 箭头，96×96 画布。"""

    # 原设计的几何：两条描边路径、三档描边宽度、三个色值。
    # 这些数字就是这枚图形本身，改了就是换了图标 —— 要换先改这条断言，
    # 并且在 PR 里写清楚是谁要换的。
    MAIN_PATH = "M22 30h26a16 16 0 0 1 0 32H46"
    HEAD_PATH = "M36 48 24 62 36 76"
    STROKES = {16: 13.5, 32: 12.5, 48: 12.0, 512: 11.0}
    COLORS = ("#4f8cff", "#7b5cff", "#d946ef")

    def _svg(self, name: str = "icon.svg") -> str:
        return (LOGO / name).read_text(encoding="utf-8")

    def test_geometry_is_the_original(self):
        """两条描边路径原样保留。

        横臂 + 右端半圆 + 回程下折走一笔，箭头是回程末端折出来的两段。
        箭头不是贴上去的三角 —— 它是这条笔画的收尾，所以没有填充路径。
        """
        svg = self._svg()
        assert self.MAIN_PATH in svg, "主体折返线被改了"
        assert self.HEAD_PATH in svg, "箭头的两段斜边被改了"

    def test_no_filled_shapes(self):
        """整枚图形只用描边，没有填充块。

        一旦出现填充块，基本就是「又贴了一个挂件上去」——
        前几版读成 Z / C / D，根子都在这。
        """
        svg = self._svg()
        assert 'fill="none"' in svg
        stripped = svg.replace('fill="none"', "")
        assert 'fill="url(#gMain)"' not in stripped, "出现了填充图形，箭头又被做成挂件了"

    def test_single_gradient_with_brand_colors(self):
        svg = self._svg()
        assert svg.count("<linearGradient") == 1
        for color in self.COLORS:
            assert color in svg.lower(), f"品牌色 {color} 丢了"

    def test_stroke_scales_with_size_not_shape(self):
        """小尺寸加粗一档是为了标签栏上看得清，骨架不变。

        这三档在生成器里是常量：改了等于把它变成另一枚图标
        （16px 版放大后和 512px 并排看会对不上）。
        """
        from scripts import gen_logo as gen

        for size, width in self.STROKES.items():
            assert gen.stroke_for(size) == width, f"{size}px 的描边宽度变了"

    def test_preview_shows_real_pixels_next_to_blowups(self):
        """小尺寸的糊是看不出来的：得把真实像素和放大并排放，才看得见骨架。"""
        html = (LOGO / "preview.html").read_text(encoding="utf-8")
        assert "小尺寸实测" in html
        assert "pixelated" in html


class TestLogoLooksLikeTheOriginal:
    """不逐像素比，比「缩到缩略图还像不像」。

    参考图在测试里用 Pillow 的 ImageDraw 现画：圆头粗线是 Pillow 自带的能力，
    不需要 cairo。之前这版用 cairosvg 现画，本地有 libcairo、CI 镜像里没有，
    于是本地绿、CI 红——测的是环境不是代码。
    """

    SIZE = 96
    SUPERSAMPLE = 4

    @staticmethod
    def _cov(im: Image.Image, size: int = SIZE) -> Image.Image:
        """把「墨迹覆盖率」这张 L 图裁到包围盒再归一化尺寸，两个来源才可比。"""
        if im.mode == "RGBA":
            im = im.getchannel("A")
        box = im.point(lambda v: 255 if v > 8 else 0).getbbox()
        assert box, "图形是空图"
        return im.crop(box).resize((size, size), Image.LANCZOS)

    def _reference(self):
        """按 scripts/gen_logo.py 里的两条路径手绘参考图（96×96 画布，描边 11）。

        Pillow 的 line/arc 不带圆头，也不共享端点圆心，所以：
        - 主体按「外圈弧 → 内圈弧」拼出那个环，再接两段直线；
        - 每条折线的拐点补一个圆点当 linejoin，两端各补一个当 round cap。
        """
        n, k = self.SIZE * self.SUPERSAMPLE, self.SUPERSAMPLE
        im = Image.new("L", (n, n), 0)
        d = ImageDraw.Draw(im)
        w = 11 * k

        def circle(cx, cy, r):
            d.ellipse([cx * k - r, cy * k - r, cx * k + r, cy * k + r], fill=255)

        # 半圆环：外半径 21.5、内半径 10.5，圆心 (48, 46)
        r_out, r_in = 11 / 2 + 16, 16 - 11 / 2
        cx, cy = 48, 46
        d.arc([(cx - r_out) * k, (cy - r_out) * k, (cx + r_out) * k, (cy + r_out) * k],
              start=-90, end=90, fill=255, width=int(11 * k))
        _ = r_in

        # 横臂与回程：从左端 (22,30) 穿过圆心高度，到下折段 (46,62)
        d.line([(22 * k, 30 * k), (cx * k, 30 * k)], fill=255, width=int(w))
        d.line([(cx * k, 62 * k), (46 * k, 62 * k)], fill=255, width=int(w))

        # 箭头两段斜边
        d.line([(36 * k, 48 * k), (24 * k, 62 * k)], fill=255, width=int(w))
        d.line([(24 * k, 62 * k), (36 * k, 76 * k)], fill=255, width=int(w))

        # round cap：每笔的两端各一个半径 5.5 的圆
        r = 11 / 2
        for x, y in ((22, 30), (46, 62), (36, 48), (36, 76), (24, 62)):
            circle(x, y, r)

        # 别在这里缩：缩完再取包围盒，抗锯齿会把画布撑满，裁出来就错位了。
        return im

    def test_current_icon_matches_original_geometry(self):
        """icon-512.png 与「按同一几何手绘」的参考图比对。

        判据是形状重叠率（IoU），不是逐像素相等：渲染器一换，抗锯齿就抖一两像素，
        逐像素比会天天假警报。

        阈值 0.75：参考图是 Pillow 手绘的，线宽只能取整、圆头是另补的圆点，
        描边会比真渲染粗半个像素。这种系统性偏胖吃掉约两成重叠率，
        但换成 S 骨架 / 两道折臂那类「另一枚图形」，重叠率会掉到 0.3 以下，
        中间有足够宽的判别带。
        """
        now = self._cov(Image.open(LOGO / "icon-512.png").convert("RGBA"))
        want = self._cov(self._reference())

        a = [v > 8 for v in now.getdata()]
        b = [v > 8 for v in want.getdata()]
        inter = sum(1 for x, y in zip(a, b) if x and y)
        union = sum(1 for x, y in zip(a, b) if x or y)
        iou = inter / union
        assert iou > 0.75, f"和原设计的形状重叠只有 {iou:.1%}，图形被改过了"


class TestWordmarkInk:
    """横版字标的颜色必须焊死在文件里。

    上一版用 currentColor：被 `<img src>` 引用时拿不到页面 color，
    落到初始值黑色，深底上整行字看不见。自查页上一眼就能看到，
    但没人会天天开自查页，所以钉成断言。
    """

    def test_no_current_color_in_wordmark(self):
        for name in ("logo-horizontal.svg", "logo-horizontal-dark.svg"):
            text = (LOGO / name).read_text(encoding="utf-8")
            assert "currentColor" not in text, f"{name} 还在用 currentColor，<img> 引用时会变黑"

    def test_two_inks_are_file_level_not_media_query(self):
        """深浅两份拆成两个文件，不靠 prefers-color-scheme。

        媒体查询由渲染器求值：`<img>` 里未必生效，光栅化器更是直接忽略，
        最终会静默落到浅色那一版 —— 这比「必须挑一份」更难查。
        """
        light = (LOGO / "logo-horizontal.svg").read_text(encoding="utf-8")
        dark = (LOGO / "logo-horizontal-dark.svg").read_text(encoding="utf-8")
        assert "prefers-color-scheme" not in light and "prefers-color-scheme" not in dark
        assert light != dark, "两份字标内容一样，说明深色版没真出"
        # 两份都别留白色底板
        for text in (light, dark):
            assert "<rect" not in text

    def test_preview_pairs_each_ink_with_its_background(self):
        html = (LOGO / "preview.html").read_text(encoding="utf-8")
        assert "logo-horizontal-dark.svg" in html, "深底那段没换成浅色字标"
