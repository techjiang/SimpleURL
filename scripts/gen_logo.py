"""生成 SimpleURL 的 Logo 资源（纯透明背景，无底板）。

造型：一条「折返线」从右上绕回，再把去路折成一个箭头 —— 长链接在这里被绕短。
- 无底板、无边框，任何底色都能直接用；
- 渐变沿对角方向铺，让折返处自然衔接深蓝 → 紫 → 品红。

输出：
- icon.svg / icon-{16..512}.png    主图标（方形画布，图形随尺寸调整描边粗细）
- favicon.svg / ../favicon.ico     站点图标（画布收窄，小尺寸更醒目）
- icon-mono.svg                    单色版，currentColor
- logo-horizontal.svg              图标 + 字标横版
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "app" / "static" / "logo"
OUT.mkdir(parents=True, exist_ok=True)

BLUE = "#4f8cff"
PURPLE = "#7b5cff"
PINK = "#d946ef"

# 折返 + 箭头，坐标固定在 96×96 画布上（图形实际占用 x 18..80, y 24..82）
BODY = (
    'M22 30h26a16 16 0 0 1 0 32H46',
    'M36 48 24 62 36 76',
)
VIEW_TIGHT = "6 14 84 76"  # favicon 用，去掉多余留白

# 描边随渲染尺寸微调：小图加粗，免得糊成一团
STROKE_STEPS = ((16, 13.5), (32, 12.5), (48, 12), (96, 11), (10**9, 11))


def stroke_for(size: int) -> float:
    for limit, width in STROKE_STEPS:
        if size <= limit:
            return width
    return 11


def _grad(kind: str) -> str:
    if kind == "mono":
        paint = "currentColor"
    else:
        paint = "url(#gMain)"
    return paint


def _defs(kind: str) -> str:
    if kind == "mono":
        return ""
    return (
        '<defs><linearGradient id="gMain" x1="0" y1="1" x2="1" y2="0">'
        f'<stop offset="0" stop-color="{BLUE}"/>'
        f'<stop offset=".55" stop-color="{PURPLE}"/>'
        f'<stop offset="1" stop-color="{PINK}"/>'
        "</linearGradient></defs>"
    )


def _paths(stroke: float, kind: str = "grad") -> str:
    paint = _grad(kind)
    body = "".join(
        f'\n  <path d="{d}" fill="none" stroke="{paint}" stroke-width="{stroke:g}"'
        ' stroke-linecap="round" stroke-linejoin="round"/>'
        for d in BODY
    )
    return body


def make_icon_svg(stroke: float = 11) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="96" height="96"'
        ' role="img" aria-label="SimpleURL">'
        f"\n  {_defs('grad')}{_paths(stroke)}\n</svg>\n"
    )


def make_favicon_svg(stroke: float = 13) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{VIEW_TIGHT}" width="96" height="87"'
        ' role="img" aria-label="SimpleURL">'
        f"\n  {_defs('grad')}{_paths(stroke)}\n</svg>\n"
    )


def make_mono_svg(stroke: float = 11) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="96" height="96"'
        ' role="img" aria-label="SimpleURL">'
        f"{_paths(stroke, 'mono')}\n</svg>\n"
    )


def make_wordmark_svg(stroke: float = 11) -> str:
    # 字标用 currentColor，深底浅底都跟着走；只有 URL 三个字母锁品牌色
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 356 96" width="356" height="96"'
        ' role="img" aria-label="SimpleURL">\n'
        f"  {_defs('grad')}{_paths(stroke)}\n"
        '  <text x="104" y="61" font-family="Inter, \'SF Pro Display\', -apple-system, \'Segoe UI\', '
        "'PingFang SC', system-ui, sans-serif\" font-size=\"44\" font-weight=\"700\" letter-spacing=\"-1.4\""
        f' fill="currentColor">Simple<tspan fill="{BLUE}">URL</tspan></text>\n</svg>\n'
    )


PNG_SIZES = (16, 32, 48, 64, 96, 128, 180, 192, 256, 512)


def render_pngs() -> list[str]:
    import cairosvg

    names: list[str] = []
    for size in PNG_SIZES:
        svg = make_icon_svg(stroke_for(size)).encode()
        name = f"icon-{size}.png"
        cairosvg.svg2png(bytestring=svg, write_to=str(OUT / name), output_width=size, output_height=size)
        names.append(name)

    # favicon.ico：用收窄画布出图，小尺寸更清楚
    ico_sizes = (16, 32, 48)
    ico_imgs = []
    for size in ico_sizes:
        svg = make_favicon_svg(stroke_for(size) + 2).encode()
        path = OUT / f".favicon-{size}.png"
        cairosvg.svg2png(bytestring=svg, write_to=str(path), output_width=size, output_height=size)
        ico_imgs.append((size, path))

    from PIL import Image

    imgs = [(size, Image.open(path).convert("RGBA")) for size, path in ico_imgs]
    imgs[0][1].save(
        OUT.parent / "favicon.ico",
        format="ICO",
        sizes=[(s, s) for s, _ in imgs],
        append_images=[img for _, img in imgs[1:]],
    )
    for _, path in ico_imgs:
        path.unlink()
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
