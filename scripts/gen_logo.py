"""生成 SimpleURL 的 Logo 资源（纯透明背景，无底板）。

造型：**折返**。一条线向右绕回来，再把去路折成一支指向左上的箭头 ——
长链接在这里被折短，这就是产品做的全部事情。

为什么换掉上一版：上一版也是一条折返线，但它只有「一段弧 + 一个折角」，
缩到 32px 以下就散成一堆断开的笔画，而且在浅色底上看着像个缺口。
这一版把主笔拉成一条完整的水平长笔，折返处用半圆收口，箭头独立成笔，
重心落在左下，任何尺寸都有一个能认出来的轮廓。

输出：
- icon.svg / icon-{16..512}.png    主图标（方形画布，描边随尺寸分档）
- favicon.svg / ../favicon.ico     站点图标（画布收紧，小尺寸更醒目）
- icon-mono.svg                    单色版，currentColor
- logo-horizontal.svg              图标 + 字标横版

改配色改下面三个色值，改造型改 MAIN / ARROW 两条路径，再跑一次本脚本。
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "app" / "static" / "logo"
OUT.mkdir(parents=True, exist_ok=True)

BLUE = "#4f8cff"
PURPLE = "#7b5cff"
PINK = "#e04ff0"

# 正稿画在 112×112 上：水平长笔 -> 右侧半圆折返 -> 指向左上的箭头
MAIN = "M32 37 H67 A21.5 21.5 0 0 1 67 80"
ARROW = "M64 62 L45 81 L64 100"
STROKE = 12

# 描边随渲染尺寸分档：小图加粗，免得糊成一团
STROKE_STEPS = ((16, 17), (24, 15.5), (32, 14.2), (48, 13.2), (96, 12.4), (10**9, 12))

# favicon 收紧画布：主图标留白是按 112 画布的视觉重心算的，缩到 16px 就显得图形偏小
FAVICON_VIEW = "20 25 81 87"

PNG_SIZES = (16, 32, 48, 64, 96, 128, 180, 192, 256, 512)


def stroke_for(size: int) -> float:
    for limit, width in STROKE_STEPS:
        if size <= limit:
            return width
    return STROKE


def _defs() -> str:
    return (
        '<defs><linearGradient id="g" x1="0.04" y1="1" x2="0.96" y2="0">'
        f'<stop offset="0" stop-color="{BLUE}"/>'
        f'<stop offset=".5" stop-color="{PURPLE}"/>'
        f'<stop offset="1" stop-color="{PINK}"/>'
        "</linearGradient></defs>"
    )


def _body(stroke: float, paint: str = "url(#g)") -> str:
    return (
        f'<path d="{MAIN}" fill="none" stroke="{paint}" stroke-width="{stroke:g}" stroke-linecap="round"/>'
        f'<path d="{ARROW}" fill="none" stroke="{paint}" stroke-width="{stroke:g}"'
        ' stroke-linecap="round" stroke-linejoin="round"/>'
    )


def make_icon_svg(stroke: float = STROKE) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 112 112" width="112" height="112"'
        f' role="img" aria-label="SimpleURL">{_defs()}{_body(stroke)}</svg>\n'
    )


def make_favicon_svg(stroke: float = 13.5) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{FAVICON_VIEW}" width="112" height="120"'
        f' role="img" aria-label="SimpleURL">{_defs()}{_body(stroke)}</svg>\n'
    )


def make_mono_svg(stroke: float = STROKE) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 112 112" width="112" height="112"'
        f' role="img" aria-label="SimpleURL">{_body(stroke, "currentColor")}</svg>\n'
    )


def make_wordmark_svg(stroke: float = STROKE) -> str:
    # 字标用 currentColor，深底浅底都跟着走；只有 URL 三个字母锁品牌色
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 380 112" width="380" height="112"'
        ' role="img" aria-label="SimpleURL">'
        f"{_defs()}{_body(stroke)}"
        '<text x="124" y="70" font-family="Inter, \'SF Pro Display\', -apple-system, \'Segoe UI\', '
        "'PingFang SC', system-ui, sans-serif\" font-size=\"46\" font-weight=\"700\""
        f' letter-spacing="-1.6" fill="currentColor">Simple<tspan fill="{BLUE}">URL</tspan></text></svg>\n'
    )


def render_pngs() -> list[str]:
    import cairosvg

    names: list[str] = []
    for size in PNG_SIZES:
        svg = make_icon_svg(stroke_for(size)).encode()
        name = f"icon-{size}.png"
        cairosvg.svg2png(bytestring=svg, write_to=str(OUT / name), output_width=size, output_height=size)
        names.append(name)

    # favicon.ico：用收紧画布出图，16px 下图形能占满
    from PIL import Image

    ico_imgs = []
    for size in (16, 32, 48):
        path = OUT / f".ico-{size}.png"
        cairosvg.svg2png(
            bytestring=make_favicon_svg(stroke_for(size) + 1.5).encode(),
            write_to=str(path),
            output_width=size,
            output_height=size,
        )
        ico_imgs.append((size, Image.open(path).convert("RGBA")))
    ico_imgs[0][1].save(
        OUT.parent / "favicon.ico",
        format="ICO",
        sizes=[(s, s) for s, _ in ico_imgs],
        append_images=[img for _, img in ico_imgs[1:]],
    )
    for size, _ in ico_imgs:
        (OUT / f".ico-{size}.png").unlink()
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
