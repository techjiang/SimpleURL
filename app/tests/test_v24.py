"""v24：一次把站点真跑起来、挨着页面看过去之后修的东西。

共同点是「测试全绿、功能也对，但屏幕上不对劲」：
开了代理就建不了短链、访客一进首页控制台就红一行、
统计图上 14 天的日期挤在角落糊成一片、结果卡片里飘一句没头没尾的话。
"""

from __future__ import annotations

import re
from pathlib import Path

import ipaddress
import pytest

from app.core import security

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
ADMIN_JS = (ROOT / "static" / "admin.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


def block(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert m, f"找不到规则：{selector}"
    return m.group(1)


class TestSsrfKeepsProxiedUsersWorking:
    """目标地址校验：代理/VPN 把域名解析到保留段时不该判成「地址不合法」。"""

    def test_literal_ip_still_strict(self):
        # 用户明着填的地址，私网/回环/链路本地/CGNAT 一律拒
        for addr in ("10.0.0.5", "127.0.0.1", "169.254.1.1", "100.64.1.1", "192.168.1.1", "0.0.0.0"):
            assert not security.is_public_ip(ipaddress.ip_address(addr)), addr

    def test_literal_reserved_ip_rejected(self):
        # 保留段手填也不收：那不是公网，也不该有人这么填
        assert not security.is_public_ip(ipaddress.ip_address("198.18.0.186"))
        assert not security.is_public_ip(ipaddress.ip_address("192.0.2.1"))

    def test_resolved_reserved_ip_allowed(self):
        # 域名解析出来的保留段放行：198.18/15 是常见的本地代理分流域，
        # 一律拒等于「开了代理就建不了短链」
        assert security.is_public_ip(ipaddress.ip_address("198.18.0.186"), literal=False)
        assert security.is_public_ip(ipaddress.ip_address("192.0.2.1"), literal=False)

    def test_resolved_private_ip_still_rejected(self):
        # 解析出来的内网地址照旧拦：10.0.0.1.nip.io 这类自带解析的域名不能绕过
        for addr in ("10.0.0.5", "127.0.0.1", "169.254.1.1", "100.64.1.1", "::1", "fe80::1"):
            assert not security.is_public_ip(ipaddress.ip_address(addr), literal=False), addr

    def test_ipv4_mapped_v6_checked_by_inner_v4(self):
        assert not security.is_public_ip(ipaddress.ip_address("::ffff:10.0.0.1"), literal=False)
        assert not security.is_public_ip(ipaddress.ip_address("::ffff:127.0.0.1"), literal=False)

    def test_never_public_list_does_not_drift_with_python(self):
        # ipaddress 在 3.13 前后对 100.64/10 归类不一致，挡 SSRF 不能跟着解释器版本漂
        nets = {str(n) for n in security._NEVER_PUBLIC}
        for cidr in ("10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "fc00::/7", "fe80::/10"):
            assert cidr in nets, cidr

    def test_url_keeps_literal_flag_for_host(self):
        # 字面 IP 的那条分支不许传 literal=False，否则用户能手填 198.18.x 绕过去
        src = (ROOT / "core" / "security.py").read_text(encoding="utf-8")
        assert "return is_public_ip(ip)" in src
        assert "resolves_to_internal(host, literal=False)" in src


class TestGuestConsoleStaysQuiet:
    """访客进首页时不该为管理员接口白撞一串 401。"""

    def test_dup_check_skipped_for_guest(self):
        body = APP_JS.split("function bindDupCheck()", 1)[1].split("function ", 1)[0]
        assert "if (!access.is_admin)" in body

    def test_links_and_tags_skipped_for_guest(self):
        links = APP_JS.split("async function loadLinks()", 1)[1].split("async function ", 1)[0]
        assert "renderGuestBoard()" in links
        assert "if (!access.is_admin)" in links


class TestResultFacts:
    """结果卡片：标题带出来了，就别再补一句「标题已自动抓取」。"""

    def test_manual_title_not_announced(self):
        assert "link.title_source !== 'manual'" in APP_JS

    def test_title_lands_in_facts(self):
        assert 'result-title' in APP_JS

    def test_empty_facts_collapsed(self):
        assert "is-empty" in APP_JS
        assert "display: none" in block(".result-facts.is-empty")


class TestChartLabelsReadable:
    """统计图的日期：以前挤出滚动容器被裁掉，或者全挤在最后一根柱子上。"""

    def test_labels_anchored_right_not_centered(self):
        body = block(".bar b")
        # 居中会让 14px 宽的柱子扛一个 29px 的标签，左右都压到邻柱
        assert "transform: translateX" not in body
        assert "right: 0" in body

    def test_labels_not_pushed_outside_scroller(self):
        # top:100% 会落到 .chart-scroll 的裁剪区外，横轴下面一个字都没有
        assert "top: 100%" not in block(".bar b")

    def test_bar_reserves_room_for_label(self):
        assert "margin-bottom: 16px" in block(".bar")

    def test_bar_width_has_both_bounds(self):
        body = block(".bar")
        assert "min-width: 20px" in body
        assert "max-width: 34px" in body

    def test_bars_no_longer_squeezed(self):
        # flex basis 为 0 时 30 根柱子会被摊成 5px 宽的细线
        assert "flex: 1 0 auto" in block(".bar")
        assert "flex: 1 0 auto" not in block(".chart-bars")

    def test_per_label_width_matches_label_metrics(self):
        # 标签实测 29px 宽，抽稀按 26px 计算才不会糊成一条
        assert "const per = 26;" in ADMIN_JS
        assert "const per = 26;" in APP_JS


class TestNoLayoutRegressions:
    def test_skip_link_does_not_cover_title(self):
        # 位移法把链接挪出屏幕后它仍占着「视口内位置」，截图里会压在标题上；
        # 靠 clip-path 裁掉才是真的不占地方（transform 只留着居中）
        body = block(".skip-link")
        assert "clip-path: inset(0 0 100% 0)" in body
        assert "top: -100" not in body
        assert "left: -9999" not in body

    def test_select_options_are_dark(self):
        # 少了 color-scheme，弹层里 rgba 底会被当成「没设置」，退回系统白底白字
        assert "select { color-scheme: dark; }" in CSS
        m = re.search(r"select option,\s*select optgroup \{([^}]*)\}", CSS)
        assert m, "找不到下拉项配色"
        assert "#0c1322" in m.group(1) and "#e8edf7" in m.group(1)

    def test_chart_axis_has_no_backdrop(self):
        body = block(".chart-axis")
        assert "background" not in body

    def test_result_facts_centered(self):
        assert "align-content: center" in block(".result-facts")
