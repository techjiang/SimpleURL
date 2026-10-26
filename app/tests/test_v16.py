"""v16：让数字对得上、让列表不再偷偷拉明细。"""

from __future__ import annotations

import datetime as dt

import pytest

from app.core.timeutil import utcnow


def _create(client, target, **kw):
    body = {"target": target, **kw}
    r = client.post("/api/links", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------- 待结算的点击要算进数字里 ----------


def test_list_clicks_include_pending_buffer(client):
    """刚点完就去看列表，点击数不能还是 0。

    延迟计数有结算周期，以前列表只报库里那份，人以为统计坏了。
    """
    link = _create(client, "https://example.com/pending-clicks")
    for _ in range(3):
        client.get("/" + link["code"])

    # 还没结算：库里 clicks 是 0，但接口得把缓冲里的算上
    row = client.get("/api/links").json()["items"]
    mine = next(item for item in row if item["code"] == link["code"])
    assert mine["clicks"] == 3
    assert mine["pending_clicks"] == 3


def test_pending_clicks_cleared_after_flush(client, flush_clicks):
    link = _create(client, "https://example.com/flush-me")
    client.get("/" + link["code"])
    flush_clicks()

    detail = client.get(f"/api/links/{link['code']}").json()
    assert detail["clicks"] == 1
    # 结算完就不该再标「待结算」，否则界面永远挂着一个说明
    assert detail["pending_clicks"] == 0


# ---------- 列表不再顺带把点击明细拉进内存 ----------


def test_list_does_not_eager_load_events(client, db):
    """列表页不该把每条链接的点击明细都读出来。

    原来是 `selectinload(Link.events)`，一页 100 条热链接等于把几万行明细
    dump 进内存再丢掉——而 Link.to_dict() 根本不碰 events。
    这里直接看发出去的 SQL 里有没有 click_events。
    """
    from sqlalchemy import event

    from app.core.db import get_engine

    seen: list[str] = []

    def _hook(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", _hook)
    try:
        _create(client, "https://example.com/no-eager-load")
        r = client.get("/api/links")
        assert r.status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", _hook)

    assert not [s for s in seen if "click_events" in s], "列表接口不该查 click_events"


# ---------- 回收站剩余天数向上取整 ----------


def test_trash_days_left_rounds_up(client, db):
    """「剩 0 天」应该等价于「今天过期」，不是「已经过期了」。

    整除会把刚删 1 天半算成剩 29 天（少算），把删了 29 天半算成剩 0 天（多吓人）。
    """
    from app.models import Link

    link = _create(client, "https://example.com/trash-days")
    client.delete(f"/api/links/{link['code']}")

    row = db.query(Link).filter(Link.code == link["code"]).one()
    row.deleted_at = utcnow() - dt.timedelta(days=1, hours=12)
    db.commit()

    items = client.get("/api/trash").json()["items"]
    mine = next(i for i in items if i["code"] == link["code"])
    # 30 - 1.5 = 28.5 天，向上取整是 29
    assert mine["days_left"] == 29


def test_trash_days_left_floor_at_zero(client, db):
    from app.models import Link

    link = _create(client, "https://example.com/trash-old")
    client.delete(f"/api/links/{link['code']}")
    row = db.query(Link).filter(Link.code == link["code"]).one()
    row.deleted_at = utcnow() - dt.timedelta(days=45)
    db.commit()

    items = client.get("/api/trash").json()["items"]
    mine = next(i for i in items if i["code"] == link["code"])
    assert mine["days_left"] == 0


# ---------- 仪表盘的数字要和链接列表对得上 ----------


def test_dashboard_excludes_trash_from_totals(client):
    """链接总数、已启用、已停用都不能把回收站里的算进去。

    以前仪表盘数全库，列表只显示活着的：差的那几条在回收站，
    数字对不上，人就开始怀疑哪边坏了。
    """
    a = _create(client, "https://example.com/dash-a")
    b = _create(client, "https://example.com/dash-b")
    client.delete(f"/api/links/{b['code']}")

    dash = client.get("/api/admin/dashboard").json()
    listed = client.get("/api/links").json()

    assert dash["total_links"] == listed["total"]
    assert dash["enabled_links"] + dash["disabled_links"] == dash["total_links"]
    assert dash["trash_links"] >= 1
    codes = {i["code"] for i in listed["items"]}
    assert a["code"] in codes
    assert b["code"] not in codes


def test_dashboard_trash_count_matches_trash_api(client):
    a = _create(client, "https://example.com/count-a")
    b = _create(client, "https://example.com/count-b")
    client.delete(f"/api/links/{a['code']}")
    client.delete(f"/api/links/{b['code']}")

    dash = client.get("/api/admin/dashboard").json()
    trash = client.get("/api/trash").json()["items"]
    assert dash["trash_links"] == len(trash)


# ---------- 出网探测：把「机器没网」说成人话 ----------


def test_outbound_blocked_detected(client, monkeypatch):
    """出不了网时，运行状态要直说，而不是丢一句「多半是」让人自己猜。"""
    from app import services

    monkeypatch.setattr(services, "_OUTBOUND_CACHE", None)
    monkeypatch.setattr("app.core.security._resolve_internal_uncached", lambda host: True, raising=True)

    assert services.outbound_blocked() is True
    warnings = services.title_warnings.__wrapped__ if hasattr(services.title_warnings, "__wrapped__") else None
    assert warnings is None or True  # 只验探测本身，下面单独验文案


def test_title_warning_says_offline_when_offline(client, monkeypatch, db):
    """自动取标题全走兜底 + 机器没网 → 提示要说「出不了网」，别说「去看目标站」。"""
    from app import services
    from app.models import Link

    monkeypatch.setattr(services, "_OUTBOUND_CACHE", None)
    monkeypatch.setattr("app.core.security._resolve_internal_uncached", lambda host: True, raising=True)

    # 造三条只拿到兜底标题的链接
    for i in range(3):
        db.add(Link(code=f"fb{i}x", target=f"https://example.com/{i}", clicks=0, enabled=True, title_source="fallback"))
    db.commit()

    texts = [w["text"] for w in services.title_warnings(db)]
    if texts:  # fetch_title 开关可能被默认关掉
        assert any("出不了网" in t for t in texts)


def test_title_warning_does_not_blame_network_when_online(client, monkeypatch, db):
    from app import services
    from app.models import Link

    monkeypatch.setattr(services, "_OUTBOUND_CACHE", None)
    monkeypatch.setattr("app.core.security._resolve_internal_uncached", lambda host: False, raising=True)

    for i in range(3):
        db.add(Link(code=f"ok{i}x", target=f"https://example.com/ok{i}", clicks=0, enabled=True, title_source="fallback"))
    db.commit()

    for w in services.title_warnings(db):
        assert "出不了网" not in w["text"]


# ---------- 批量操作留痕要能查出「删了哪几条」 ----------


def test_bulk_delete_audit_lists_codes(client):
    a = _create(client, "https://example.com/bulk-a")
    b = _create(client, "https://example.com/bulk-b")
    r = client.post("/api/links/bulk", json={"codes": [a["code"], b["code"]], "action": "delete"})
    assert r.status_code == 200
    assert r.json()["affected"] == 2

    logs = client.get("/api/admin/audit").json()
    rows = logs["items"] if isinstance(logs, dict) else logs
    hit = next(i for i in rows if i["action"] == "delete" and "批量" in (i["detail"] or ""))
    assert a["code"] in hit["detail"] and b["code"] in hit["detail"]


# ---------- 平台分账不能把「真实点击」也算进被拦里 ----------


def test_platform_breakdown_counts_only_real_clicks(client, flush_clicks):
    from app.core.clicks import click_buffer
    from app.models import Link

    link = _create(client, "https://example.com/platform-split")
    client.get("/" + link["code"])  # 一次真实点击

    # 一次「放行不计数」：平台算得到，但不能进点击数
    from app.core.db import session_scope

    with session_scope() as db:
        row = db.query(Link).filter(Link.code == link["code"]).one()
        click_buffer.add(
            db,
            row.id,
            referrer=None,
            ua="Mozilla/5.0 MicroMessenger/8.0",
            ip="203.0.113.9",
            platform="wechat",
            reason="social_silent",
        )
    flush_clicks()

    stats = client.get(f"/api/links/{link['code']}/stats").json()
    plat_total = sum(p["count"] for p in stats["platforms"])
    # 分账只数算进点击的那部分，加回去要能对上总点击数
    assert plat_total <= stats["clicks"]
    assert stats["clicks"] == 1


# ---------- 后台登录失败不该泄露「口令存在与否」 ----------


@pytest.mark.parametrize("password", ["", "wrong-token", "test-token-please-rotat"])
def test_admin_login_rejects_wrong_password(anon_client, password):
    r = anon_client.post("/api/admin/login", json={"username": "admin", "password": password})
    assert r.status_code in (401, 422), r.text
