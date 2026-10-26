"""v17：品牌一致性、解析超时、创建链路上的锁。

这三个都是「用起来才会撞见」的那类问题：功能没坏、测试全绿，
但部署在真实环境里会一步步变成事故。所以每一条都先复现再钉住。
"""

from __future__ import annotations

import socket
import threading
import time

import pytest


class TestManifestFollowsBrand:
    """PWA 清单不能是张死文件。

    `app/static/manifest.webmanifest` 是静态资源，后台把站名改成别的之后，
    首页标题、页脚、分享卡片全换了，可「添加到主屏幕」之后图标下面
    还印着出厂的 SimpleURL —— 用户看到的是两个品牌，而没有任何页面提示他。
    """

    def test_manifest_is_served_and_parses(self, client):
        r = client.get("/manifest.webmanifest")
        assert r.status_code == 200
        assert "manifest" in r.headers["content-type"]
        body = r.json()
        assert body["start_url"] == "."
        assert body["icons"]

    def test_manifest_follows_site_name(self, client):
        r = client.post("/api/admin/settings", json={"site_name": "云间短链"})
        assert r.status_code == 200
        assert client.get("/manifest.webmanifest").json()["name"] == "云间短链"

    def test_manifest_description_follows_tagline(self, client):
        client.post("/api/admin/settings", json={"site_tagline": "自建的才安心"})
        assert client.get("/manifest.webmanifest").json()["description"] == "自建的才安心"

    def test_static_dead_file_is_gone(self, client):
        # 留着那张死文件，任何一次「顺手改回来」都会让上面两条测试变成假绿
        from app.core.config import settings

        assert not (settings.static_dir / "manifest.webmanifest").exists()


class TestSettingsPanelShowsEffectiveValue:
    """后台面板上写的必须是「正在生效的值」。

    `get_all()` 抄的是 `EDITABLE` 里那列写死的默认值，`effective()` 却认
    环境变量。于是 `SITE_NAME=我的站` 部署起来，页面全对，后台打开一看
    站点名称写着 SimpleURL —— 更糟的是点一下保存，就真把它改成 SimpleURL 了。
    """

    def test_env_site_name_reaches_panel(self, client, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "site_name", "环境里的站名")
        assert client.get("/api/admin/settings").json()["site_name"] == "环境里的站名"

    def test_env_announcement_reaches_panel(self, client, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "announcement", "今晚 02:00 维护")
        assert client.get("/api/admin/settings").json()["announcement"] == "今晚 02:00 维护"

    def test_db_value_still_wins(self, client, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "site_name", "环境里的站名")
        client.post("/api/admin/settings", json={"site_name": "库里改过的"})
        assert client.get("/api/admin/settings").json()["site_name"] == "库里改过的"


class TestAnnouncementRendersServerSide:
    def test_announcement_visible_before_js_runs(self, client, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "announcement", "本周六凌晨维护")
        html = client.get("/").text
        assert "本周六凌晨维护" in html
        # 空的公告不该留一个占着位置的空条
        client.post("/api/admin/settings", json={"announcement": ""})
        # 清空之后整条收起：留一个空条会白占一行版面
        assert 'id="notice-bar" role="status" hidden' in client.get("/").text


class TestDnsResolutionIsBounded:
    """域名解析必须有硬超时。

    `getaddrinfo` 是同步阻塞调用，域名服务器不回话时要等系统解析器的
    timeout（默认 5 秒，还带重试）。它跑在创建链接的请求线程里，
    于是「DNS 半死不活」的时候，每次创建都先卡几秒，并发一上来
    同步线程池就被占满——整站跟着变慢，而页面只显示「创建有点慢」。
    """

    def test_slow_dns_gives_up_quickly(self, monkeypatch):
        from app.core import security
        from app.core.config import settings

        # conftest 为了离线跑测试把 SSRF_RESOLVE_CHECK 关了，这里单独打开
        monkeypatch.setattr(settings, "ssrf_resolve_check", True)
        monkeypatch.setattr(settings, "dns_timeout", 0.3)
        security._dns_cache.pop("slow.test", None)

        def slow(*_a, **_kw):
            time.sleep(5)
            raise socket.gaierror("boom")

        monkeypatch.setattr(socket, "getaddrinfo", slow)
        started = time.monotonic()
        # 超时按「不安全」处理：宁可拒收，也不把「解析不出来」当公网放过去
        assert security.is_public_url("https://slow.test/x") is False
        assert time.monotonic() - started < 2.0

    def test_dns_failure_counts_as_unsafe(self, monkeypatch):
        from app.core import security
        from app.core.config import settings

        monkeypatch.setattr(settings, "ssrf_resolve_check", True)
        security._dns_cache.pop("nxdomain.test", None)

        def boom(*_a, **_kw):
            raise socket.gaierror("no such host")

        monkeypatch.setattr(socket, "getaddrinfo", boom)
        assert security.is_public_url("https://nxdomain.test/x") is False

    def test_private_resolution_still_blocked(self, monkeypatch):
        from app.core import security
        from app.core.config import settings

        monkeypatch.setattr(settings, "ssrf_resolve_check", True)
        security._dns_cache.pop("internal.test", None)
        monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_kw: [(2, 1, 6, "", ("10.0.0.7", 0))])
        assert security.is_public_url("https://internal.test/x") is False




class TestLogoAssets:
    """Logo 是产品门面，缺一张图就是一次 404。"""

    @pytest.mark.parametrize("size", [16, 32, 48, 64, 96, 128, 180, 192, 256, 512])
    def test_all_png_sizes_exist_with_alpha(self, client, size):
        from PIL import Image

        from app.core.config import settings

        path = settings.static_dir / "logo" / f"icon-{size}.png"
        assert path.exists(), f"缺 icon-{size}.png"
        img = Image.open(path)
        assert img.mode == "RGBA"
        assert img.size == (size, size)
        # 无背景 = 四角必须是透明的，否则贴到深色页面会出现一块底板
        assert img.getpixel((0, 0))[3] == 0
        assert img.getpixel((size - 1, size - 1))[3] == 0

    def test_icon_svg_and_ico_served(self, client):
        assert client.get("/static/logo/icon.svg").status_code == 200
        assert client.get("/static/logo/favicon.svg").status_code == 200
        r = client.get("/favicon.ico")
        assert r.status_code == 200
        assert "icon" in r.headers["content-type"]

    def test_apple_touch_icon_exists(self, client):
        # 模板与路由都指向 icon-180.png，文件不在的话 iOS 加主屏就是一张空白
        from app.core.config import settings

        assert (settings.static_dir / "logo" / "icon-180.png").exists()
        assert client.get("/apple-touch-icon.png").status_code == 200

class TestFallbackTitleIsMeaningful:
    """兜底标题不能是「1」。

    `https://site/-/issues/1` 的网络拿不到标题时，兜底拼出最后一段 —— 于是
    列表里每一条都顶着个「1」。这比留空更糟：留空至少一眼看出「没取到」，
    而「1」会让人以为抓到的是一个正常标题，只是内容很怪。
    """

    def test_numeric_last_segment_falls_back_to_host(self):
        from app.core.titles import guess_title

        assert guess_title("https://cnb.cool/some/repo/-/issues/1") == "cnb.cool"

    def test_meaningless_segments_fall_back_to_host(self):
        from app.core.titles import guess_title

        for url in ("https://example.com/", "https://example.com/index.html", "https://example.com/page"):
            assert guess_title(url) == "example.com"

    def test_readable_segment_still_used(self):
        from app.core.titles import guess_title

        assert guess_title("https://docs.python.org/3/library/asyncio-task.html") == "asyncio task"
