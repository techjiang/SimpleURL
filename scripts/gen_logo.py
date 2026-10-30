"""生成 SimpleURL 的 Logo 资源：无底板、纯透明底。

造型：一笔绕回。上臂向右折成大弧，再往左下收成一条斜笔 —— 长链接
在这里被绕短、折回。渐变沿对角铺（青 → 紫 → 品红），交接处自然接上。

输出（app/static/logo/）：
- icon.svg / icon-{16..512}.png   主图标
- favicon.svg / ../favicon.ico     站点图标（画布按图形收紧，小尺寸更醒目）
- icon-mono.svg                    单色版，用 currentColor
- logo-horizontal.svg              图标 + 字标横版
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "app" / "static" / "logo"
OUT.mkdir(parents=True, exist_ok=True)

CYAN, VIOLET, HOT = (48, 198, 255), (150, 108, 255), (248, 118, 196)
STOPS_SVG = ("#30c6ff", "#966cff", "#f876c4")

# 设计坐标固定 100×100，图形实际占用 x 9..78, y 17..88
UPPER = [(22, 19), (46, 19), (62, 24), (72, 34), (76, 48), (74, 62), (67, 74), (57, 82), (50, 86)]
LOWER = [(50, 86), (44, 76), (34, 64), (22, 52), (12, 44)]
# 描边随尺寸微调：小图略粗，免得糊成一团
STROKE_STEPS = ((16, 13.5), (32, 13), (48, 12.5), (10 ** 9, 12.5))
TIGHT = "4.5 10.5 82 82"  # favicon 画布，去掉多余留白


def stroke_for(size: int) -> float:
    for limit, width in STROKE_STEPS:
        if size <= limit:
            return width
    return 12.5


def _path(points: list[tuple[float, float]]) -> str:
    head, *rest = points
    return "M" + " ".join(f"{x:g} {y:g}" for x, y in (head,)) + "".join(
        f"L{x:g} {y:g}" for x, y in rest
    )


def _defs(kind: str) -> str:
    if kind == "mono":
        return ""
    return (
        '<defs><linearGradient id="gMain" x1="0" y1="0" x2="1" y2=".42">'
        f'<stop offset="0" stop-color="{STOPS_SVG[0]}"/>'
        f'<stop offset=".5" stop-color="{STOPS_SVG[1]}"/>'
        f'<stop offset="1" stop-color="{STOPS_SVG[2]}"/>'
        "</linearGradient></defs>"
    )


def _paths(stroke: float, kind: str = "grad") -> str:
    paint = "currentColor" if kind == "mono" else "url(#gMain)"
    return "".join(
        f'\n  <path d="{_path(pts)}" fill="none" stroke="{paint}" stroke-width="{stroke:g}"'
        ' stroke-linecap="round" stroke-linejoin="round"/>'
        for pts in (UPPER, LOWER)
    )


def make_icon_svg(stroke: float | None = None) -> str:
    stroke = stroke if stroke is not None else stroke_for(256)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100"'
        ' role="img" aria-label="SimpleURL">'
        f"\n  {_defs('grad')}{_paths(stroke)}\n</svg>\n"
    )


def make_favicon_svg(stroke: float = 13) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{TIGHT}" width="100" height="100"'
        ' role="img" aria-label="SimpleURL">'
        f"\n  {_defs('grad')}{_paths(stroke)}\n</svg>\n"
    )


def make_mono_svg(stroke: float = 12.5) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100"'
        ' role="img" aria-label="SimpleURL">'
        f"{_paths(stroke, 'mono')}\n</svg>\n"
    )


def make_wordmark_svg(stroke: float = 12.5) -> str:
    # 字标走 currentColor，深浅底都跟着变色；只有 URL 三个字母锁品牌色
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 360 100" width="360" height="100"'
        ' role="img" aria-label="SimpleURL">\n'
        f"  {_defs('grad')}{_paths(stroke)}\n"
        '  <text x="104" y="62" font-family="Inter, \'SF Pro Display\', -apple-system, \'Segoe UI\', '
        "'PingFang SC', system-ui, sans-serif\" font-size=\"44\" font-weight=\"700\""
        f' letter-spacing="-1.2" fill="currentColor">Simple<tspan fill="{STOPS_SVG[1]}">URL</tspan></text>\n'
        "</svg>\n"
    )


PNG_SIZES = (16, 32, 48, 64, 96, 128, 180, 192, 256, 512)


def _render_png(size: int, stroke: float) -> "object":
    """用 PIL 直接画，不依赖 cairosvg；8 倍超采样后缩回去做抗锯齿。"""
    from PIL import Image, ImageDraw

    ss = 8
    big = size * ss
    pts_sets = [
        [(x / 100 * big, y / 100 * big) for x, y in UPPER],
        [(x / 100 * big, y / 100 * big) for x, y in LOWER],
    ]
    mask = Image.new("L", (big, big), 0)
    d = ImageDraw.Draw(mask)
    r = stroke / 100 * big / 2
    for pts in pts_sets:
        d.line(pts, fill=255, width=int(stroke / 100 * big), joint="curve")
        for x, y in pts:  # line 端点不收圆角，补圆
            d.ellipse([x - r, y - r, x + r, y + r], fill=255)

    ramp = Image.new("RGB", (big, big))
    rp = ramp.load()
    for y in range(big):
        for x in range(big):
            t = min(1.0, max(0.0, x / big * 0.92 + y / big * 0.38))
            a, b, k = (CYAN, VIOLET, t / 0.5) if t < 0.5 else (VIOLET, HOT, (t - 0.5) / 0.5)
            rp[x, y] = tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))
    out = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    out.paste(ramp, (0, 0), mask)
    return out.resize((size, size), Image.LANCZOS)


def render_pngs() -> list[str]:
    from PIL import Image

    names: list[str] = []
    for size in PNG_SIZES:
        name = f"icon-{size}.png"
        _render_png(size, stroke_for(size)).save(OUT / name)
        names.append(name)

    ico = [_render_png(s, stroke_for(s) + 1) for s in (16, 32, 48)]
    ico[0].save(
        OUT.parent / "favicon.ico",
        format="ICO",
        sizes=[(i.width, i.height) for i in ico],
        append_images=ico[1:],
    )
    names.append("../favicon.ico")
    return names


def main() -> None:
    (OUT / "icon.svg").write_text(make_icon_svg(), encoding="utf-8")
    (OUT / "favicon.svg").write_text(make_favicon_svg(), encoding="utf-8")
    (OUT / "icon-mono.svg").write_text(make_mono_svg(), encoding="utf-8")
    (OUT / "logo-horizontal.svg").write_text(make_wordmark_svg(), encoding="utf-8")
    print("生成:", ", ".join(render_pngs()))


if __name__ == "__main__":
    main()
