"""v21：图表横轴抽稀、标题实体、登录态提示、顶栏溢出与卡片对齐。

这一轮都是「跑起来不报错、看着不对劲」的毛病：图表标签拿柱宽当字宽算，
一屏二十来个日期叠成灰线；网页标题里的 `&#8212;` 被原样吐到列表上；
会话过期时门里那句「请先登录」像是在怪用户没登录过；390px 下「首页」
压在品牌字上；后台两栏卡片一高一矮，右侧列表悬在半空。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
ADMIN_JS = (ROOT / "static" / "admin.js").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
ADMIN_HTML = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")


def block(css: str, selector: str) -> str:
    """取第一条匹配选择器的规则体，够用就行。"""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"找不到规则：{selector}"
    return m.group(1)


class TestChartLabels:
    def test_labels_helper_exists(self):
        # 抽稀逻辑收在一个函数里，渲染处只管取文本
        assert "function chartLabels(el, daily)" in ADMIN_JS

    def test_labels_are_map(self):
        # 用 Map 存「下标 -> 文本」：调用处再 slice 一次字符串最容易切出空标签
        assert "const out = new Map();" in ADMIN_JS
        assert "labels.has(i) ? labels.get(i) : ''" in ADMIN_JS

    def test_thin_out_keeps_first_and_last(self):
        assert "if (i === 0 || i === last || i % step === 0) mark(i);" in ADMIN_JS

    def test_room_accounts_for_label_width(self):
        # 每根柱子连缝带标签要留 26px（标签 20px + gap 6px），日期「MM-DD」还要再翻一倍多；
        # 按柱宽算而不是按柱子数算
        assert "const per = 26;" in ADMIN_JS
        assert "Math.floor(width / (per * 2.2))" in ADMIN_JS

    def test_zero_tick_not_shifted(self):
        # 旧写法在「最高就是 1」时把 0 挪走，纵轴底部空出一格
        assert "v >= 1" in ADMIN_JS
        assert "v >= 1" in APP_JS


class TestBarWidth:
    def test_bars_capped(self):
        # 上下限都定在柱子上：上限防止 14 天在宽屏被拉成色块，
        # 下限防止 30 天在窄屏细到日期标签探出图表被裁
        body = block(CSS, ".bar")
        assert "max-width: 34px" in body
        assert "min-width: 20px" in body

    def test_bars_anchor_right(self):
        # 柱子靠右排：日期从左往右读，右边的「今天」要贴住右边界
        body = block(CSS, ".chart-bars")
        assert "justify-content: flex-end" in body
        assert "gap: 6px" in body


class TestTopnav:
    def test_nav_does_not_cover_brand(self):
        # 窄屏给导航 4px 基准宽，让它自己滚，而不是按内容宽度往品牌上顶。
        # 全局那条 `.topnav` 与窄屏那条同名，取包含 flex-basis 的那条
        m = re.search(r"\.topnav \{([^}]*flex: 1 1 4px[^}]*)\}", CSS)
        assert m, "窄屏 .topnav 应给导航 4px 基准宽"
        body = m.group(1)
        assert "min-width: 0" in body
        assert "justify-content: flex-start" in body

    def test_nav_scrolls_horizontally(self):
        m = re.search(r"\.topnav \{([^}]*overflow-x: auto[^}]*)\}", CSS)
        assert m and "scrollbar-width: none" in m.group(1)


class TestAdminCols:
    def test_columns_stretch(self):
        assert "align-items: stretch" in block(CSS, ".admin-cols")

    def test_cards_fill_height(self):
        body = block(CSS, ".admin-cols > .chart-card")
        assert "flex-direction: column" in body
        assert "flex: 1 1 auto" in block(CSS, ".admin-cols .ref-list")


class TestExpiringHeader:
    def test_title_and_hint_are_separate(self):
        # 标题和说明挤在一个元素里，读屏会念成一长串
        m = re.search(r'id="dash-expiring-card".*?</div>', ADMIN_HTML, re.S)
        assert m
        chunk = m.group(0)
        assert "<h2>即将过期</h2>" in chunk
        assert "7 天内到期，趁早续上" in chunk


class TestLoginHint:
    def test_auth_flag_exists(self):
        assert "let sawAuthRequired = false;" in ADMIN_JS

    def test_hint_says_session_expired(self):
        assert "会话已过期，请重新登录" in ADMIN_JS

    def test_login_endpoint_not_treated_as_logged_out(self):
        # 密码错误也是 401，不能借机把门再放一次、盖掉真正的错误
        assert "path.startsWith('/api/admin/login')" in ADMIN_JS


class TestTitleClean:
    @pytest.mark.parametrize(
        "raw,expect",
        [
            ("A &amp; B", "A & B"),
            ("json &#8212; JSON encoder", "json \u2014 JSON encoder"),
            # 双重转义也见过：网页把 `&` 写成 `&amp;`，实体再被转一次
            ("A &amp;#8212; B", "A \u2014 B"),
            ("<b>标题</b>\n\t再一段", "标题 再一段"),
        ],
    )
    def test_clean_unescapes_entities(self, raw, expect):
        from app.core.titles import _clean

        assert _clean(raw) == expect

    def test_clean_truncates(self):
        from app.core.titles import MAX_TITLE, _clean

        assert len(_clean("字" * (MAX_TITLE + 50))) == MAX_TITLE


class TestExpiryLabel:
    def test_hours_when_less_than_a_day(self):
        # 还剩几小时的时候说「还剩 0 天」等于没说
        assert "还剩 ${h} 小时" in APP_JS or "小时" in APP_JS
        assert "不到 1 小时" in APP_JS
