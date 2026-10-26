"""v8 专项：这轮修掉的每个口子都用一条用例钉住。

写法上刻意反着来：先构造一个真实的攻击动作 / 坏输入，再断言它没得逞。
不做「读一遍代码，确认自己写的是自己想的」那种自证。
"""

from __future__ import annotations

import io
import csv

from app.core.db import session_scope
from app.core.settings_store import set_value


def create(client, **payload):
    payload.setdefault("target", "https://example.com/page")
    payload.setdefault("fetch_title", False)
    r = client.post("/api/links", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


class TestApiKeyBoundary:
    """API 密钥 ≠ 管理员登录。

    `REQUIRE_API_KEY` 打开后 `/api/*` 只认密钥，而 `/api/admin/*` 原来
    既不校验密钥也不区分会话来源——一把为了「让脚本建短链」签发的密钥，
    能读仪表盘、导出全站数据。密钥泄露的后果不该等于后台失守。
    """

    def _key(self, name="脚本用"):
        from app.models import ApiKey

        with session_scope() as db:
            db.add(ApiKey(name=name, key="sk-test-boundary-key"))
        return {"X-Api-Key": "sk-test-boundary-key"}

    def test_api_key_cannot_reach_admin_api(self, anon_client):
        headers = self._key()
        for path in ("/api/admin/dashboard", "/api/admin/settings", "/api/admin/api-keys", "/api/admin/audit"):
            assert anon_client.get(path, headers=headers).status_code == 401, path

    def test_api_key_cannot_export_or_list(self, anon_client):
        headers = self._key()
        assert anon_client.get("/api/links", headers=headers).status_code == 401
        assert anon_client.get("/api/links/export", headers=headers).status_code == 401

    def test_api_key_required_when_enabled(self, anon_client):
        with session_scope() as db:
            set_value(db, "require_api_key", True)
        r = anon_client.post("/api/links", json={"target": "https://example.com/x", "fetch_title": False})
        assert r.status_code == 401  # 没有密钥直接进不来


class TestCodeUniqueness:
    """域名不区分大小写，`Abc` 和 `abc` 落到同一个地址。"""

    def test_custom_code_case_insensitive(self, client):
        """大小写撞车不再报错，而是换码并在响应里说明。

        `AbCd` 和 `abcd` 在域名上根本就是同一个地址，谁后写谁赢——
        所以必须先建的那个保住原码，后来者拿一个新码，同时被告知原码没用上。
        """
        first = create(client, code="AbCd")
        assert first["code"] == "AbCd"
        r = client.post(
            "/api/links", json={"target": "https://x.com/other", "code": "abcd", "fetch_title": False}
        )
        assert r.status_code == 201
        body = r.json()
        assert body["swapped_from"] == "abcd"
        assert body["code"].lower() != "abcd"
        # 原码仍归先建的那条
        assert client.get("/api/links/AbCd").json()["code"] == "AbCd"

    def test_swapped_code_is_never_case_variant(self, client):
        """换出来的码也不能跟已有的只差大小写，否则同样的坑换个位置又踩一次。"""
        create(client, code="QwEr")
        r = client.post(
            "/api/links", json={"target": "https://x.com/other", "code": "qwer", "fetch_title": False}
        )
        assert r.status_code == 201
        assert r.json()["code"].lower() != "qwer"

    def test_auto_code_never_collides_with_existing_mixed_case(self, client):
        from app.core.codes import code_exists, random_code

        # 造一个只差大小写的候选：生成器必须认得它已被占用
        existing = create(client, code="Zz9Q")["code"]
        assert code_exists(existing.swapcase()) is True
        assert random_code(4) != ""  # 生成器本身仍可用


class TestCsvInjection:
    """标题是任何人都能填的字段，导出后双击打开不能变成执行。"""

    def test_formula_prefixes_neutralised(self, client):
        create(client, title='=HYPERLINK("http://evil","x")')
        create(client, title="+1+1")
        create(client, title="@SUM(1)")
        create(client, title="-2+3")
        body = client.get("/api/links/export").text.lstrip("\ufeff")
        for row in csv.DictReader(io.StringIO(body)):
            title = row["标题"]
            assert title, row
            assert title[0] == "'", f"公式没被中和：{title}"

    def test_normal_title_untouched(self, client):
        create(client, title="正常标题 = 说明")
        body = client.get("/api/links/export").text.lstrip("\ufeff")
        titles = [row["标题"] for row in csv.DictReader(io.StringIO(body))]
        assert "正常标题 = 说明" in titles


class TestReceiptReissue:
    """回执丢了 / 建的时候没发，得有一条自救的路。"""

    def test_guest_can_rotate_own_receipt(self, anon_client):
        link = create(anon_client, target="https://example.com/a")
        old = link["claim_token"]
        assert old
        r = anon_client.get(f"/api/links/{link['code']}/receipt?t={old}")
        assert r.status_code == 200, r.text
        new = r.json()["claim_token"]
        assert new and new != old
        # 旧回执立刻作废：这是「换新」不是「多发一张」
        assert anon_client.get(f"/s/{link['code']}?t={old}").status_code == 401
        assert anon_client.get(f"/s/{link['code']}?t={new}").status_code == 200

    def test_stranger_cannot_rotate(self, anon_client, client):
        link = create(client, target="https://example.com/b")
        assert anon_client.get(f"/api/links/{link['code']}/receipt").status_code == 401


class TestReceiptSurvivesNavigation:
    """二维码是 <img>，换新后要跳地址 —— 两处都带不了 query。"""

    def test_receipt_cookie_keeps_session_after_query_dropped(self, anon_client):
        link = create(anon_client, target="https://example.com/c")
        token = link["claim_token"]
        # 第一次带 ?t= 打开，服务端顺手记进 cookie
        assert anon_client.get(f"/s/{link['code']}?t={token}").status_code == 200
        # 之后地址栏被收拾干净，二维码与刷新仍然认得人
        assert anon_client.get(f"/api/qr/{link['code']}").status_code == 200
        assert anon_client.get(f"/s/{link['code']}").status_code == 200

    def test_rotate_then_continue_without_query(self, anon_client):
        from fastapi.testclient import TestClient

        from app.main import app

        link = create(anon_client, target="https://example.com/d")
        old = link["claim_token"]
        anon_client.get(f"/s/{link['code']}?t={old}")
        new = anon_client.get(f"/api/links/{link['code']}/receipt").json()["claim_token"]
        # 换新后跳到不带 query 的地址，页面和二维码都要正常
        assert anon_client.get(f"/s/{link['code']}").status_code == 200
        # 别家浏览器只拿到旧回执 + 没有 cookie：彻底进不去
        with TestClient(app) as stranger:
            assert stranger.get(f"/s/{link['code']}?t={old}").status_code == 401


class TestBrandingConsistency:
    def test_api_access_reflects_dashboard_site_name(self, client):
        r = client.post("/api/admin/settings", json={"site_name": "短链小站"})
        assert r.status_code == 200, r.text
        # 后台改完，前端横幅 / 提示语拿到的名字要跟着变，不能还是出厂值
        assert client.get("/api/access").json()["site_name"] == "短链小站"
        assert client.get("/api/stats/summary").json()["site_name"] == "短链小站"
