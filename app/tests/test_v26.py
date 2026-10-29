"""v26：这一轮全是「屏幕上的东西说不清自己在说什么」。

1. 首页热门榜一行塞了五样东西，1440 下中间空出大半屏；而访客明明刚建过
   一条、榜上那条可能就是自己建的，界面上却一个字都不说。
2. 接口页把 19 个接口全折起来，访客得先猜分组再点三下。
3. 创建区「每日上限」和它的长说明并排，那一行比别的行高出一截。
4. 统计页「创建时间」把 2026-10-29 17:47 折成两行，卡片被撑得比旁边高。
"""

from __future__ import annotations

import re
import sqlite3  # noqa: F401  # 保证内存库驱动可用
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
APP_CSS = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
TOOLS_HTML = (ROOT / "templates" / "tools.html").read_text(encoding="utf-8")
STATS_HTML = (ROOT / "templates" / "stats.html").read_text(encoding="utf-8")


def _fill_clicks(code: str, times: int = 3) -> None:
    """把点击灌进内存缓冲并结算：榜单只认 Link.clicks > 0。"""
    from app.core.clicks import click_buffer
    from app.core.db import get_sessionmaker
    from app.models import Link
    from app.services import flush_clicks

    db = get_sessionmaker()()
    link = db.query(Link).filter(Link.code == code).one()
    for _ in range(times):
        click_buffer.add(db, link.id, None, "Mozilla/5.0 (Macintosh)", "1.2.3.4", schedule=False)
    flush_clicks()
    db.close()


class TestBoardMarksOwner:
    """榜单要能认出「这条是我建的」——只回一个布尔，不回凭据。"""

    def test_owner_flag_true_for_own_link(self, anon_client):
        created = anon_client.post("/api/links", json={"target": "https://example.com/mine"}).json()
        code, token = created["code"], created["claim_token"]
        assert token
        _fill_clicks(code)

        items = anon_client.get("/api/board", headers={"Cookie": f"su_claim={token}"}).json()["items"]
        mine = [i for i in items if i["code"] == code]
        assert mine and mine[0]["owner"] is True

    def test_owner_flag_false_without_claim(self, anon_client):
        created = anon_client.post("/api/links", json={"target": "https://example.com/anonymous"}).json()
        _fill_clicks(created["code"])
        # 创建时服务端会往这个客户端种 su_claim，所以要显式清掉才算「没有回执的访客」。
        # 不清就成了「带着回执来问」，测的也不是这条了。
        anon_client.cookies.pop("su_claim", None)
        items = anon_client.get("/api/board").json()["items"]
        row = [i for i in items if i["code"] == created["code"]][0]
        assert row["owner"] is False

    def test_other_peoples_claim_does_not_leak(self, anon_client):
        mine = anon_client.post("/api/links", json={"target": "https://example.com/a"}).json()
        other = anon_client.post("/api/links", json={"target": "https://example.com/b"}).json()
        _fill_clicks(mine["code"])
        _fill_clicks(other["code"])
        items = anon_client.get("/api/board", headers={"Cookie": f"su_claim={other['claim_token']}"}).json()["items"]
        flags = {i["code"]: i["owner"] for i in items}
        assert flags[other["code"]] is True
        assert flags[mine["code"]] is False

    def test_board_never_returns_the_claim_itself(self, anon_client):
        created = anon_client.post("/api/links", json={"target": "https://example.com/secret"}).json()
        _fill_clicks(created["code"])
        raw = anon_client.get("/api/board", headers={"Cookie": f"su_claim={created['claim_token']}"}).text
        assert created["claim_token"] not in raw
        assert "claim" not in raw


class TestBoardRowLayout:
    """一行三样东西：名次、链接、点击数。短码自己就是复制按钮。"""

    def test_code_is_the_copy_button(self):
        assert 'class="hotboard-code copy-code"' in APP_JS
        # 行尾那个独立按钮已经去掉：要复制的就是短码，不该让人做配对
        assert "hotboard-copy" not in APP_JS
        assert "hotboard-copy" not in APP_CSS

    def test_mine_badge_only_when_owner(self):
        assert "hotboard-mine" in APP_JS
        assert "item.owner" in APP_JS

    def test_guest_empty_text_points_at_board(self):
        # 榜上已经有自己那条了，就别再说「这里什么都没有」
        assert "boardHasMine" in APP_JS
        assert "「我的」标记那条" in APP_JS


class TestToolsEndpointList:
    """接口清单默认展开：这页的用处就是「请求怎么发」。"""

    def test_details_open_by_default(self):
        assert re.search(r'<details class="api-group" open>', TOOLS_HTML)

    def test_header_shows_real_count(self):
        assert "endpoints.values() | map('length') | sum" in TOOLS_HTML


class TestComposerLayout:
    """每日上限和备注并排：各自独占一行会留下两段空档。"""

    def test_daily_limit_and_note_share_row(self):
        block = INDEX_HTML[INDEX_HTML.index('id="daily_limit"'):]
        block = block[: block.index('id="clue"')]
        # 中间夹着找回线索那行的 <div class="row">，所以数到 1 就说明
        # 上限和备注之间没有另起一行
        assert 'id="note"' in block
        assert block.count('class="row"') == 1


class TestStatsCards:
    """两张时间卡必须和数字卡一样高。"""

    def test_time_cards_use_compact(self):
        assert STATS_HTML.count('strong class="compact"') >= 2

    def test_compact_style_exists(self):
        assert ".stat-card strong.compact" in APP_CSS
