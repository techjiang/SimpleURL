"""子命令一致性与 Logo 造型的验收线（v29）。

这一轮两件事：

1. **帮助里写的子命令，照着敲必须能用。** `./install.sh status` 以前会落到
   「未知参数」上 —— 帮助里列得清清楚楚，照着敲却全报错。这类不一致比
   缺功能更伤信任，所以逐条过一遍。
2. **Logo 得读得出是 S。** 上一版透明、尺寸一致、不贴边都过了，但图形读作 Z：
   两道横臂末端各切一个尖角，像两个挂件。这版只留下道末端一个箭头，
   S 靠两道半圆立住。断言钉住「只有一处箭头」「只有一个渐变」。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from PIL import Image

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


class TestLogoSaysS:
    def _svg(self, name: str) -> str:
        return (LOGO / name).read_text(encoding="utf-8")

    def test_only_one_arrow_head(self):
        """上一版在两道横臂末端各切一刀，等于两枚三角挂在两端，读作 Z。

        现在只有下道末端一个箭头：填充路径（没有 stroke 的那条）只该有一条。
        """
        svg = self._svg("icon.svg")
        filled = re.findall(r"<path[^>]*fill=\"url\(#g\)\"(?!\s*stroke)", svg)
        assert len(filled) == 1, f"填充路径有 {len(filled)} 条，箭头不该重复出现"

    def test_single_gradient(self):
        svg = self._svg("icon.svg")
        assert svg.count("<linearGradient") == 1

    def test_gradient_is_the_calm_one(self):
        """青→品红那套高饱和渐变压到 16px 上色带打架，这版收成同一色系。"""
        svg = self._svg("icon.svg")
        for hot in ("#35d0ff", "#ff5ac8", "#d946ef"):
            assert hot not in svg.lower(), f"还留着过艳的色值 {hot}"

    def test_mono_pngs_come_in_both_inks(self):
        """单色 PNG 必须是「颜色焊死在文件里」的两份。

        currentColor 在 cairosvg 里没有继承源，出图全黑，贴深底等于没图标。
        """
        for name in ("icon-mono-32.png", "icon-mono-light-32.png"):
            im = Image.open(LOGO / name).convert("RGBA")
            assert im.getchannel("A").getbbox(), f"{name} 是空图"
        dark = Image.open(LOGO / "icon-mono-32.png").convert("RGBA")
        light = Image.open(LOGO / "icon-mono-light-32.png").convert("RGBA")
        box = dark.getchannel("A").getbbox()
        assert dark.getpixel(((box[0] + box[2]) // 2, box[1]))[0] < 100
        assert light.getpixel(((box[0] + box[2]) // 2, box[1]))[0] > 200

    def test_preview_shows_real_pixels_next_to_blowups(self):
        """小尺寸的糊是看不出来的：得把真实像素和放大并排放，才看得见骨架。"""
        html = (LOGO / "preview.html").read_text(encoding="utf-8")
        assert "小尺寸实测" in html
        assert "pixelated" in html


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


class TestGeneratedCliHygiene:
    """生成的 simpleurl 命令里，注释也会被外层 heredoc 先执行一遍。

    真实踩过两次：注释里写一句 `simpleurl password`，安装时被当成命令替换，
    整行替换成多行输出，剩下的半句成了多行注释 —— 后面的函数全被注掉。
    屏幕上一句 No such file or directory，看着完全不像「注释写错了」。
    """

    CLI_HEREDOC = re.compile(r'tee "\$cli" >/dev/null <<EOF\n(.*?)\nEOF', re.S)

    def _body(self) -> str:
        m = self.CLI_HEREDOC.search(INSTALL.read_text(encoding="utf-8"))
        assert m, "找不到生成管理命令的 heredoc"
        return m.group(1)

    def test_no_bare_backticks(self):
        assert "`" not in self._body(), "生成的命令里有反引号，安装时会被当成命令替换执行"

    def test_every_dollar_is_escaped(self):
        """正文里的 $ 全都要写成 \\$：heredoc 没加引号，漏一个就在安装时展开。"""
        body = self._body()
        raw = re.findall(r"(?<!\\)\$\(", body)
        assert not raw, f"有 {len(raw)} 处未转义的 $( ，安装时会被提前执行"

    def test_password_without_args_is_read_only(self):
        """`simpleurl password` 是最常敲的一条，敲一下就把密码作废太伤。

        默认只看，--reset 才换新的。
        """
        body = self._body()
        assert "show_password" in body
        assert "--reset" in body
