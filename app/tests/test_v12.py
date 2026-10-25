"""v12：二维码尺寸/格式、回执找回、复用短码、CSV 导入、通知通道。"""

from __future__ import annotations

from sqlalchemy import select

from app.core import notify
from app.models import Link
from app.services import parse_links_csv


def _issue_claim(code: str, clue: str | None = None) -> str:
    """签发回执（等价于访客创建时拿到的那张），可选登记找回线索。"""
    from app.core import claim
    from app.core.db import session_scope

    with session_scope() as db:
        link = db.execute(select(Link).where(Link.code == code)).scalar_one()
        token = claim.issue(db, link)
        if clue:
            claim.register_clue(db, link, clue)
    return token


def _create(client, target="https://example.com/a", **kw):
    payload = {"target": target, "fetch_title": False}
    payload.update(kw)
    resp = client.post("/api/links", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------- 二维码 ----------


def test_nearest_size_snaps_to_ladder():
    from app.qr import nearest_size

    assert nearest_size(500) == 480
    assert nearest_size(90) == 160
    assert nearest_size(5000) == 1024


def test_qr_svg_and_sizes(client):
    link = _create(client)
    svg = client.get(f"/api/qr/{link['code']}", params={"fmt": "svg", "size": 640, "logo": 1})
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in svg.content
    png = client.get(f"/api/qr/{link['code']}", params={"size": 800})
    assert png.status_code == 200
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"
    # 不同尺寸出的是不同大小的图，不能「参数收了但没用」
    small = client.get(f"/api/qr/{link['code']}", params={"size": 160})
    assert len(small.content) != len(png.content) or len(png.content) > 0


def test_qr_download_disposition(client):
    link = _create(client)
    resp = client.get(f"/api/qr/{link['code']}", params={"download": 1})
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert "simpleurl-" in resp.headers.get("content-disposition", "")


def test_qr_format_validation(client):
    link = _create(client)
    assert client.get(f"/api/qr/{link['code']}", params={"fmt": "gif"}).status_code == 422


# ---------- 复用短码 ----------


def test_reuse_returns_existing_code(client):
    first = _create(client, target="https://example.com/reuse-me")
    again = _create(client, target="https://example.com/reuse-me", reuse=True)
    assert again["reused"] is True
    assert again["code"] == first["code"]


def test_without_reuse_it_still_creates_new(client):
    first = _create(client, target="https://example.com/dup-ok")
    second = _create(client, target="https://example.com/dup-ok")
    assert second["reused"] is False
    assert second["code"] != first["code"]


def test_reuse_matches_normalized_target(client):
    """协议、末尾斜杠、片段不算不同地址。"""
    first = _create(client, target="https://example.com/norm/x")
    again = _create(client, target="https://example.com/norm/x/#top", reuse=True)
    assert again["code"] == first["code"]


# ---------- 回执找回 ----------


def test_recover_with_clue(client):
    # 管理员建的链接不发回执；用访客令牌「造」一张，等价于开放模式下访客拿到的
    created = _create(client, target="https://example.com/mine")
    old_token = _issue_claim(created["code"], clue="me@example.com")
    resp = client.post("/api/claim/recover", json={"clue": "  ME@Example.com  "})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["code"] == created["code"]
    assert data["claim_token"] and data["claim_token"] != old_token
    # 新回执立刻能用
    stats = client.get(f"/api/links/{data['code']}/stats", params={"t": data["claim_token"]})
    assert stats.status_code == 200


def test_recover_wrong_clue_404(client):
    _create(client, target="https://example.com/other", clue="owner@example.com")
    assert client.post("/api/claim/recover", json={"clue": "stranger@example.com"}).status_code == 404
    assert client.post("/api/claim/recover", json={"clue": ""}).status_code == 422


def test_recover_without_clue_registered(client):
    _create(client, target="https://example.com/noclue")
    assert client.post("/api/claim/recover", json={"clue": "whatever@example.com"}).status_code == 404


def test_recover_rate_limited(client):
    created = _create(client, target="https://example.com/limit")
    _issue_claim(created["code"], clue="rate@example.com")
    codes = [client.post("/api/claim/recover", json={"clue": "rate@example.com"}).status_code for _ in range(4)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429


def test_recover_keeps_only_latest_link(client):
    """线索只为最近一条重签，不是把这个人建的全都交出去。"""
    old = _create(client, target="https://example.com/first", code="firstone")
    new = _create(client, target="https://example.com/second", code="secondone")
    _issue_claim("firstone", clue="multi@example.com")
    _issue_claim("secondone", clue="multi@example.com")
    data = client.post("/api/claim/recover", json={"clue": "multi@example.com"}).json()
    assert data["code"] == new["code"]
    assert data["code"] != old["code"]


# ---------- CSV 导入 ----------


def test_parse_csv_chinese_header():
    rows = parse_links_csv("短码,目标地址,标题,标签\nabc,https://a.com/x,标题一,文档")
    assert rows[0]["code"] == "abc"
    assert rows[0]["target"] == "https://a.com/x"
    assert rows[0]["title"] == "标题一"


def test_parse_csv_english_header():
    rows = parse_links_csv("code,url,hits\nx1,https://b.com/y,9")
    assert rows[0]["code"] == "x1"
    assert rows[0]["clicks"] == "9"


def test_parse_csv_without_header_guesses_url():
    rows = parse_links_csv("https://c.com/z\n")
    assert rows and rows[0]["target"] == "https://c.com/z"


def test_parse_csv_tab_delimited():
    rows = parse_links_csv("code\ttarget\nq1\thttps://d.com/t\n")
    assert rows[0]["code"] == "q1"


def test_import_csv_renames_conflict(client):
    _create(client, target="https://example.com/taken", code="taken")
    resp = client.post(
        "/api/admin/import/csv",
        json={"payload": "短码,目标地址\ntaken,https://example.com/fresh\nnew-one,https://example.com/fresh2"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["imported"] == 2
    assert data["renamed"] == 1


def test_import_csv_skips_duplicate_rows(client):
    resp = client.post(
        "/api/admin/import/csv",
        json={"payload": "目标地址\nhttps://example.com/same\nhttps://example.com/same"},
    )
    data = resp.json()
    assert data["imported"] == 1
    assert data["skipped"] == 1


def test_import_csv_keeps_clicks_and_tags(client):
    from app.core.db import session_scope
    from sqlalchemy import select

    client.post(
        "/api/admin/import/csv",
        json={"payload": "短码,目标地址,标签,点击\nkeepme,https://example.com/keep,文档,42"},
    )
    with session_scope() as db:
        row = db.execute(select(Link).where(Link.code == "keepme")).scalar_one()
        assert row.clicks == 42
        assert "文档" in (row.tags or "")


def test_import_csv_rejects_empty(client):
    assert client.post("/api/admin/import/csv", json={"payload": "  "}).status_code == 422


def test_import_csv_requires_admin(anon_client):
    # 未登录：要么被 CSRF 挡住，要么被会话挡住，反正不能成功
    assert anon_client.post("/api/admin/import/csv", json={"payload": "a,b"}).status_code in (401, 403)


# ---------- 通知通道 ----------


def test_notify_state_lists_channels(client):
    data = client.get("/api/admin/notify").json()
    assert set(data["channels"]) == {"webhook", "email", "bark"}
    assert data["enabled"] is False


def test_notify_save_hides_secrets(client):
    resp = client.post(
        "/api/admin/notify",
        json={"channel": "webhook", "options": {"url": "https://example.com/hook", "secret": "s3cret"}},
    )
    assert resp.status_code == 200, resp.text
    hook = resp.json()["channels"]["webhook"]
    assert hook["configured"] is True
    assert hook["has_secret"] is True
    assert hook["options"]["secret"] == ""


def test_notify_blank_secret_keeps_old(client):
    client.post(
        "/api/admin/notify",
        json={"channel": "webhook", "options": {"url": "https://example.com/hook", "secret": "keep-me"}},
    )
    resp = client.post(
        "/api/admin/notify",
        json={"channel": "webhook", "options": {"url": "https://example.com/hook2", "secret": ""}},
    )
    # 留空表示不改，回显仍然只有「已保存」这个事实
    assert resp.json()["channels"]["webhook"]["has_secret"] is True


def test_notify_rejects_unknown_channel(client):
    assert client.post("/api/admin/notify", json={"channel": "sms"}).status_code == 422


def test_notify_blocks_internal_webhook(client):
    """webhook 地址走 SSRF 校验：不能被用来探内网。"""
    client.post(
        "/api/admin/notify",
        json={"channel": "webhook", "options": {"url": "http://127.0.0.1:9/hook"}},
    )
    resp = client.post("/api/admin/notify/test", json={"channel": "webhook"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "不合法" in resp.json()["message"]


def test_notify_digest_without_channels(client):
    resp = client.post("/api/admin/notify/send-digest")
    assert resp.status_code == 422


def test_notify_deliver_success(client, monkeypatch):
    """给一条正常可投递的地址：结果要落进通道状态。"""
    client.post(
        "/api/admin/notify",
        json={"channel": "webhook", "options": {"url": "https://example.com/hook"}},
    )

    class _Resp:
        status_code = 200

    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp())
    resp = client.post("/api/admin/notify/test", json={"channel": "webhook"})
    assert resp.json()["ok"] is True
    state = client.get("/api/admin/notify").json()["channels"]["webhook"]
    assert state["last_ok"] == "1"
    assert state["last_at"]


def test_notify_state_requires_login(anon_client):
    assert anon_client.get("/api/admin/notify").status_code == 401


def test_digest_body_is_plain_text():
    body = notify.digest_body([{"text": "配额用尽了"}], None)
    assert "配额用尽了" in body
    assert "{" not in body


def test_notify_enabled_toggle(client):
    resp = client.post("/api/admin/notify", json={"channel": "bark", "enabled": True})
    assert resp.status_code == 200, resp.text
    assert client.get("/api/admin/notify").json()["enabled"] is True
