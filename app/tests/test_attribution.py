"""署名是写死的：后台改不动，环境变量也改不动。"""

from __future__ import annotations

SIGN_URL = 'href="https://github.com/techjiang/SimpleURL"'


def test_footer_signature_is_fixed(guest):
    html = guest.get("/").text
    assert "Powered by" in html
    assert SIGN_URL in html
    assert ">SimpleURL</a>" in html


def test_signature_has_no_admin_option():
    from app.core import runtime

    assert all("attribut" not in k and "powered" not in k for k in runtime.EDITABLE_KEYS)


def test_cannot_override_signature_via_settings(client):
    r = client.post("/api/admin/settings", json={"attribution_text": "Hacked", "powered_by": "x"})
    assert r.status_code == 200
    assert r.json()["applied"] == {}
    assert "Hacked" not in client.get("/").text


def test_signature_survives_site_name_change(client):
    client.post("/api/admin/settings", json={"site_name": "MyShort"})
    try:
        html = client.get("/").text
        assert ">SimpleURL</a>" in html
        assert SIGN_URL in html
    finally:
        client.post("/api/admin/settings", json={"site_name": "SimpleURL"})
