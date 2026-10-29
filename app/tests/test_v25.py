"""v25：又把站点真跑了一遍，挑出来的都属「字面上没错、用起来别扭」。

1. 额度按**本地**自然日切，文案却写「UTC 零点重置」。东八区用户的额度
   其实在本地零点就回满了，屏幕上却告诉他再等八小时。
2. 本地记着的回执短码只增不减：自助删除之后那条短码还留在 localStorage，
   下次一进首页就去请求一个永远 401 的接口，「我的链接」永远是空的。
3. 网络抖一下（500/超时）也被当成「回执没了」，同一个 401 分支顺手把
   本地记录抹掉——一次断网就把这台设备上唯一的入口丢了。
4. 改完每日额度，顶部结果卡还挂着「额度用尽」：同一屏两个说法。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
ADMIN_JS = (ROOT / "static" / "admin.js").read_text(encoding="utf-8")
PAGES = (ROOT / "pages.py").read_text(encoding="utf-8")
MODELS = (ROOT / "models.py").read_text(encoding="utf-8")


def js_block(src: str, header: str) -> str:
    """取 js 里某个 `function xxx(` 到它闭合的 `}` 之间的正文。"""
    start = src.index(header)
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"没找到闭合块：{header}")


class TestQuotaCopyMatchesImplementation:
    """额度日切是本地日，文案就不能写 UTC。"""

    def test_no_utc_midnight_in_ui(self):
        # 前端三个地方 + 后端的 429 页面都说过「UTC 零点重置」
        for name, src in (("app.js", APP_JS), ("admin.js", ADMIN_JS), ("pages.py", PAGES)):
            assert "UTC 零点" not in src, f"{name} 仍在说「UTC 零点」"

    def test_quota_page_says_local_midnight(self):
        assert "明天零点会重新计算额度" in PAGES

    def test_quota_day_cut_is_local(self):
        # 判定侧与写入侧必须同一个口径，否则东八区用户会在早上 8 点回满
        assert "today_local()" in MODELS
        src = (ROOT / "core" / "clicks.py").read_text(encoding="utf-8")
        assert "today_local()" in js_block(src, "    def _add_daily_usage")


class TestClaimCodeLifecycle:
    """本地那张「最近一条回执」只在真作废时才消失。"""

    def test_forget_helper_exists(self):
        assert "function forgetClaimCode()" in APP_JS

    def test_self_delete_forgets_code(self):
        # 自助删除 = 回执作废，本地那个短码就成了死入口
        block = js_block(APP_JS, "    del.addEventListener(")
        assert "forgetClaimCode()" in block

    def test_network_jitter_keeps_code(self):
        # 只有明确 401/404 才清；500 与断网留着下次再试
        block = js_block(APP_JS, "  async function loadGuestLinks()")
        assert "err.status === 401 || err.status === 404" in block
        assert re.search(r"catch \(_\)[\s\S]{0,240}removeItem", block) is None

    def test_api_error_carries_status(self):
        # 判断依据得是状态码，不是那句给人看的中文
        assert "err.status = res.status" in APP_JS


class TestResultCardFollowsEdit:
    """改完额度，顶层那张卡上的说法也要跟着变。"""

    def test_edit_refreshes_self_tools(self):
        bind = js_block(APP_JS, "  function bindEdit()")
        assert "refreshSelfTools(editing.code)" in bind

    def test_refresh_tolerates_failure(self):
        block = js_block(APP_JS, "  async function refreshSelfTools(")
        assert "catch" in block
