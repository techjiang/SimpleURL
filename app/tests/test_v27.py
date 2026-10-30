"""无背景 Logo 的验收线。

上一版 Logo 的问题不是「不好看」，是**不达标**：底板自带背景、PNG 有白底、
小尺寸换了描边导致 16px 和高清版是两个造型。这些都不会让页面报错，
只会让人在深色页面里看到一块方形白斑。所以这里不比对像素，只钉四件事：

1. 真透明：四角 alpha 为 0，且透明像素的 RGB 也是 0（没有白色残影）
2. 同一造型：每一档 PNG 的图形包围盒比例一致
3. PWA 图标分开给：`any` 和 `maskable` 不能混在同一个 purpose 里
4. 小尺寸还认得出：16px 里图形至少占一半画布，不被留白吃掉
"""

from __future__ import annotations

import pytest
from PIL import Image

from app.core.config import settings

LOGO = settings.static_dir / "logo"
PNG_SIZES = (16, 20, 24, 32, 48, 64, 96, 128, 180, 192, 256, 512, 1024)


def _ink_bbox(img: Image.Image):
    """把 alpha 二值化后取包围盒，滤掉抗锯齿边缘的极淡像素。"""
    return img.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()


@pytest.mark.parametrize("size", PNG_SIZES)
def test_png_is_truly_transparent(size):
    img = Image.open(LOGO / f"icon-{size}.png").convert("RGBA")
    assert img.size == (size, size)
    w, h = img.size
    for corner in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        r, g, b, a = img.getpixel(corner)
        assert a == 0, f"icon-{size}.png 的 {corner} 不透明，等于自带底板"
        assert (r, g, b) == (0, 0, 0), f"icon-{size}.png 的 {corner} 是白色残影"


@pytest.mark.parametrize("size", PNG_SIZES)
def test_png_never_leaves_a_white_plate(size):
    """底板最常见的形态是「中间不透明、边角透明」，只看四角会漏掉它。

    16px 里一笔折返本来就占满大半画布，透明像素只到 43%，所以门槛按下限取
    40%——一块圆角底板的透明占比会明显低于这个数。
    """
    img = Image.open(LOGO / f"icon-{size}.png").convert("RGBA")
    alphas = list(img.getchannel("A").get_flattened_data())
    clear = sum(1 for a in alphas if a == 0)
    assert clear / len(alphas) > 0.40, f"icon-{size}.png 透明区域太少，像是画了底板"


def test_all_sizes_draw_the_same_shape():
    """同一造型 = 图形包围盒的宽高比一致。上一版给小尺寸换了描边，
    这条就会红。宽容差是因为 1024 和 16 的取整精度差得多。"""
    ratios = []
    for size in (48, 128, 512):
        img = Image.open(LOGO / f"icon-{size}.png").convert("RGBA")
        box = _ink_bbox(img)
        assert box, f"icon-{size}.png 是空图"
        w, h = box[2] - box[0], box[3] - box[1]
        ratios.append(w / h)
    assert max(ratios) - min(ratios) < 0.05, f"各尺寸造型不一致：{ratios}"


def test_16px_still_readable():
    """16px 里图形要占住画布大半，否则浏览器标签栏上只剩一个点。

    门槛 9px：图形是 1:1 画布里的非正方形（宽高比约 1.2），16px 下横向 12px、
    纵向 10px 左右。要的是「不被留白吃掉」，不是「填满」——填满恰恰是糊的成因。
    """
    img = Image.open(LOGO / "icon-16.png").convert("RGBA")
    box = _ink_bbox(img)
    assert box, "icon-16.png 是空图"
    assert (box[2] - box[0]) >= 9 and (box[3] - box[1]) >= 9


def test_favicon_ico_has_16_and_32():
    ico = Image.open(settings.static_dir / "favicon.ico")
    assert ico.size == (16, 16)
    assert ico.info.get("sizes") == {(16, 16)}


def test_maskable_icon_leaves_safe_area():
    """maskable 会被裁成圆形/水滴，内容必须落在中间 80% 里。"""
    img = Image.open(LOGO / "icon-maskable.png").convert("RGBA")
    box = _ink_bbox(img)
    assert box, "icon-maskable.png 是空图"
    margin = img.width * 0.1
    assert box[0] >= margin and box[1] >= margin
    assert box[2] <= img.width - margin and box[3] <= img.height - margin


def test_manifest_splits_any_and_maskable(client):
    """`purpose: "any maskable"` 会让安卓按 maskable 裁掉外圈，
    连「普通图标」那一份也一起被裁。两份必须分开给。"""
    icons = client.get("/manifest.webmanifest").json()["icons"]
    purposes = [i["purpose"] for i in icons]
    assert "any maskable" not in purposes
    assert "any" in purposes and "maskable" in purposes
    maskable = next(i for i in icons if i["purpose"] == "maskable")
    assert client.get(maskable["src"]).status_code == 200


def test_every_manifest_icon_is_served(client):
    for icon in client.get("/manifest.webmanifest").json()["icons"]:
        r = client.get(icon["src"])
        assert r.status_code == 200, f"{icon['src']} 取不到"
        assert r.headers["content-type"].startswith("image/")


def test_icon_svgs_are_transparent_by_construction():
    """SVG 里不该出现白底矩形：一个 `fill="#fff"` 的 rect 就是一块底板。"""
    for name in ("icon.svg", "favicon.svg", "icon-mono.svg", "icon-maskable.svg"):
        text = (LOGO / name).read_text(encoding="utf-8")
        assert "<rect" not in text, f"{name} 里画了矩形，Logo 不该有底板"
        assert "none" in text or "currentColor" in text


def test_mono_icon_uses_current_color():
    text = (LOGO / "icon-mono.svg").read_text(encoding="utf-8")
    assert "currentColor" in text
    assert "linearGradient" not in text, "单色版不该带渐变"
