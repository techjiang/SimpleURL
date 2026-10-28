"""v23：全站文案规格与按钮规格的一致性。

这一轮没改任何功能，改的都是「跑起来没错、看着不对劲」的地方：
下载二维码长得像图片说明、设置页的字段排成 3+1、勾选框竖着占两行、
后台页签里的字被挤成两行、每张卡都挂一层 backdrop-filter 白烧 GPU。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
STATS_HTML = (ROOT / "templates" / "stats.html").read_text(encoding="utf-8")
ADMIN_HTML = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


def block(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert m, f"找不到规则：{selector}"
    return m.group(1)


class TestQrDownload:
    def test_button_looks_like_a_button(self):
        """「下载二维码」原来是一条 11.8px 的灰色虚线文字，压在二维码右下角。"""
        body = block(".qr-download")
        assert "height: 32px" in body
        assert "border-radius" in body
        assert "font-weight: 600" in body

    def test_download_hits_attachment_endpoint(self):
        """只靠 <a download> 的话，服务端没给 Content-Disposition，点完可能什么都不存。"""
        assert "download=1" in STATS_HTML

    def test_qr_sized_for_display(self):
        """图是给人看的 140px，请求按 320 出图，缩放后才不糊。"""
        assert 'width="140"' in STATS_HTML
        assert "size=320" in (ROOT / "web.py").read_text(encoding="utf-8")


class TestHiddenWins:
    def test_hidden_beats_inline_flex(self):
        """`.badge` 把 display 写死成 inline-flex，空的 `[hidden]` 会露出一个空气泡。"""
        assert "[hidden] { display: none !important; }" in CSS


class TestBlurBudget:
    def test_cards_do_not_blur(self):
        """卡片是不透明的，每张都挂 blur 只会让滚动时反复合成几十个图层。"""
        body = block(".composer, .board, .result-card, .chart-card, .stat-card")
        assert "backdrop-filter" not in body


class TestAdminTabs:
    def test_tabs_do_not_stretch_on_narrow(self):
        """窄屏 `flex: 1 1 auto` 会把四字页签挤成两行。"""
        m = re.search(r"\.admin-tab \{([^}]*flex: 0 0 auto[^}]*)\}", CSS)
        assert m, "窄屏页签应按内容宽度排"


class TestSettingsGrid:
    def test_site_name_on_its_own_row(self):
        """三个字段塞一格 3 列会剩下一格孤零零地占着第二行左边。"""
        m = re.search(r'<label for="set-site">.*?</div>', ADMIN_HTML, re.S)
        assert m and 'class="row"' not in m.group(0)

    def test_checkboxes_share_a_row(self):
        m = re.search(r'<div class="row">\s*<label class="check">.*?set-require-key.*?</label>\s*</div>',
                      ADMIN_HTML, re.S)
        assert m, "两个勾选框应并排"


class TestCopy:
    def test_no_stray_empty_label_in_composer(self):
        """表单里那个 `&nbsp;` 标签只是为了撑对齐，代价是一行看不见的空标签。"""
        assert "&nbsp;" not in INDEX_HTML


class TestStylesheetParses:
    """样式表本身得能被浏览器解析完。

    v23 那次改版在 `@media (max-width: 640px)` 里多留了一个闭括号：
    块在 `.field-note` 之后就被收掉，后面跟着三条没有归属的声明和一个孤立的
    `}`。CSS 的容错是「跳过直到下一个平衡的块」，于是**它之后的所有规则**
    都被整段丢弃——`.qr-block` 那条正好在断点下面，从此从没生效过，
    二维码旁边那条本该在正下方的按钮就一路挂在右侧。
    这类错误在源码里读起来完全正常，只有把文件真正解析一遍才能发现。
    """

    @staticmethod
    def _balanced(text: str) -> bool:
        # 注释先剥掉：`/* a { */` 里的花括号不算数
        stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        depth = 0
        for ch in stripped:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    return False
        return depth == 0

    def test_braces_balance(self):
        assert self._balanced(CSS), "app.css 花括号不配对，浏览器会从错误处开始丢掉规则"

    def test_rules_after_media_block_still_parse(self):
        """断点块之后的第一条规则必须还能被找到。"""
        head = CSS.split("/* ---------- 统计页")[0]
        assert self._balanced(head), "「统计页」那段之前就已经不配对了"


class TestQrDownloadSitsBelow:
    """要求：二维码在下、按钮在正下方，不是右边。"""

    def test_block_is_a_single_column(self):
        body = block(".qr-block")
        assert "grid-template-columns: minmax(0, max-content)" in body

    def test_button_fills_the_column(self):
        body = block(".qr-download")
        assert "width: 100%" in body
        assert "justify-content: center" in body

    def test_template_order_qr_then_button(self):
        """DOM 顺序也得是码在前、按钮在后——列宽一致时才会落到正下方。"""
        assert STATS_HTML.index('class="qr"') < STATS_HTML.index('class="qr-download"')
