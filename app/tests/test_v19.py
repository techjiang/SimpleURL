"""v19：窄屏和图表上被挤坏的那几处。

后台页签横滑时总有一个被切成半个字、链接卡片顶上的勾选框把短码顶到窄列、
卡片标题比正文还小、图表两次把刻度上限裁掉半截。
都是「桌面看着没问题、手机上或者放大看才发现」的排版缺陷。测试里没有浏览器
量不了像素，这里钉住产生布局的标记与样式本身：谁把它们改回去，测试先红。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
ADMIN_JS = (ROOT / "static" / "admin.js").read_text(encoding="utf-8")


def _block(selector: str) -> str:
    """抓某条规则的声明块，取第一条同名规则。"""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert m, f"没找到规则 {selector}"
    return m.group(1)


class TestAdminTabsWrap:
    """十来个页签塞进 390px，横滑时永远有一个被切成半个字。"""

    @staticmethod
    def _mobile() -> str:
        """所有 720px 断点的内容拼在一起，找规则不用管它在哪一个媒体查询里。"""
        blocks = re.findall(r"@media \(max-width: 720px\)\s*\{(.*?)\n\}", CSS, re.S)
        assert blocks
        return "\n".join(blocks)

    def test_mobile_tabs_wrap(self):
        block = re.search(r"\.admin-tabs\s*\{([^}]*)\}", self._mobile())
        assert block, "窄屏要给页签一条规则"
        assert "flex-wrap: wrap" in block.group(1), "窄屏的页签要折行，别再横滑"
        assert "overflow-x: auto" not in block.group(1), "折行之后不该再横滑"

    def test_tab_not_squeezed_to_single_char(self):
        """页签里的字不许折行：一折行「防洪 / UA 屏蔽」就成了三行。"""
        base = [b for b in re.findall(r"\.admin-tab\s*\{([^}]*)\}", CSS) if "white-space" in b]
        assert base and "nowrap" in base[0]


class TestLinkCard:
    """后台链接卡：勾选框把短码挤到第二列，标题比正文还小。"""

    def test_code_follows_checkbox_in_same_cell_area(self):
        """勾选框、短码、状态各占一列，短码才不会被挤到状态的窄列里。"""
        assert '.table tr:has(td[data-label="选择"]) td[data-label="短码"]' in CSS

    def test_title_inside_card_is_readable(self):
        m = re.search(
            r"\.table td\[data-label=\"短码\"\] \.title-cell\s*\{([^}]*)\}", CSS
        )
        assert m, "卡片式布局里标题需要自己的字号"
        size = float(re.search(r"font-size:\s*([\d.]+)rem", m.group(1)).group(1))
        assert size >= 0.86, "卡片标题不该比正文小一整档"


class TestChartAxis:
    """刻度上限挂在柱区上方，滚动容器的 overflow 会把它裁掉一半。"""

    def test_chart_reserves_room_above_bars(self):
        """两条 .chart 规则拼起来看：高度和 padding 得给刻度上限留出位置。"""
        blocks = re.findall(r"\.chart\s*\{([^}]*)\}", CSS)
        joined = " ".join(blocks)
        assert "padding-top" in joined, "刻度上限要位置，别让它伸到图表外面被裁"

    def test_axis_follows_bars_on_mobile(self):
        block = re.search(r"@media \(max-width: 720px\)\s*\{(.*?)\n\}", CSS, re.S)
        assert block and "height: 130px" in block.group(1), "窄屏刻度框要跟柱区一起缩"


class TestAdminTableHeaders:
    """表头一个「标题」，行里却塞了「标题 + 来源」，两边对不上。"""

    def test_header_count_matches_row_count(self):
        head = re.search(r"<tbody id=\"admin-links-body\">", (ROOT / "templates" / "admin.html").read_text(encoding="utf-8"))
        assert head
        header = re.search(r"<thead>(.*?)</thead>", (ROOT / "templates" / "admin.html").read_text(encoding="utf-8"), re.S)
        n_head = len(re.findall(r"<th\b", header.group(1)))
        row = re.search(r'<td class="col-check".*?</tr>', ADMIN_JS, re.S).group(0)
        n_cells = len(re.findall(r"<td\b", row))
        assert n_head == n_cells

    def test_title_only_rendered_once(self):
        """标题本来就在标题列，别再往短码列塞一份。"""
        # 链接管理那张表里勾选框第一列，短码第二列；回收站那张表没有勾选框
        row = re.search(r'<td class="col-check".*?<td class="col-code" data-label="短码">(.*?)</td>', ADMIN_JS, re.S).group(1)
        assert "title-cell" not in row
