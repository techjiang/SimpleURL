"""生成 SimpleURL 的 Logo 资源：无底板、纯透明底。

造型：一笔折返。上臂自左向右，绕一个大弯折回来，末端收成一个箭头，
指向来路 —— 长链接在这里被折短。整枚图形只有这一笔，没有底板、没有圆角方块。

三条硬要求，上一版都栽在它们上面：

1. **真透明**：画布上只有这一笔，alpha 之外的像素全是 0。
2. **所有尺寸是同一张图**：PNG 一律从同一条路径超采样渲染（8×）再缩回，
   不分档改描边。上一版给几个尺寸换了线宽，结果 16px 和高清版是两个造型。
3. **画布按图形包围盒算**：留白是算出来的，不是手填的 viewBox。

依赖 cairosvg（非运行时依赖，只有出图时需要）：`pip install cairosvg`。

输出（app/static/logo/）：
- icon.svg / icon-{16..1024}.png   主图标
- favicon.svg / ../favicon.ico     站点图标
- icon-mono.svg                    单色版（currentColor）
- icon-maskable.svg / .png          PWA 用，带 20% 安全区
- logo-horizontal.svg              图标 + 字标横版
- preview.html                     深底 / 浅底 / 各尺寸自查页

改色改形只动下面的常量，然后 `python scripts/gen_logo.py`。
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

OUT = Path(__file__).resolve().parents[1] / "app" / "static" / "logo"
OUT.mkdir(parents=True, exist_ok=True)

# 品牌渐变：青 → 紫 → 品红。x2/y2 是渐变的方向，斜着铺才和折返的走向对得上。
STOPS = ((0.0, "#4cc9ff"), (0.55, "#8b6bff"), (1.0, "#ff5fbf"))
GRAD_DIR = (1, 0.42)

STROKE = 13          # 设计坐标下的线宽
BODY = "M24 22H44C66 22 76 34 76 51C76 67 64 80 44 80"
ARROW = "M50 81L68 81.5L63 88.4Z"
BOX = (14, 12, 80, 80)          # 内容区：图形 + 端点圆帽，四周各 2 单位留白

PNG_SIZES = (16, 20, 24, 32, 48, 64, 96, 128, 180, 192, 256, 512, 1024)
MASKABLE_SAFE = 0.2             # PWA maskable 图标四周留的安全区比例


def _grad() -> str:
    stops = "".join(
        f'<stop offset="{o:g}" stop-color="{c}"/>' for o, c in STOPS
    )
    x2, y2 = GRAD_DIR
    return f'<linearGradient id="g" x1="0" y1="0" x2="{x2:g}" y2="{y2:g}">{stops}</linearGradient>'


def _viewbox(scale: float = 1.0) -> str:
    x, y, w, h = BOX
    if scale == 1.0:
        return f"{x:g} {y:g} {w:g} {h:g}"
    # 居中放大画布：给 maskable 留安全区
    cx, cy = x + w / 2, y + h / 2
    w2, h2 = w / scale, h / scale
    return f"{cx - w2 / 2:g} {cy - h2 / 2:g} {w2:g} {h2:g}"


def body(fill: str) -> str:
    """一笔到底：描边回路 + 末端箭头。两段同色同渐变，接缝看不出来。"""
    return (
        f'<path d="{BODY}" fill="none" stroke="{fill}" stroke-width="{STROKE:g}"'
        ' stroke-linecap="round"/>'
        f'<path d="{ARROW}" fill="{fill}"/>'
    )


def make_icon_svg(scale: float = 1.0, mono: bool = False) -> str:
    paint = "currentColor" if mono else "url(#g)"
    defs = "" if mono else f"<defs>{_grad()}</defs>"
    return (
        '<svg xmlns="http://www.w3.org/2000/svg"'
        f' viewBox="{_viewbox(scale)}" width="100" height="100"'
        ' role="img" aria-label="SimpleURL">'
        f"{defs}{body(paint)}"
        "</svg>\n"
    )


def make_wordmark_svg() -> str:
    # 字标走 currentColor，深浅底都跟着变色；只有 URL 三个字母锁品牌主色
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 372 100" width="372" height="100"'
        ' role="img" aria-label="SimpleURL">\n'
        f"  <defs>{_grad()}</defs>\n"
        f"  {body('url(#g)')}\n"
        '  <text x="104" y="62" font-family="Inter, \'SF Pro Display\', -apple-system, \'Segoe UI\','
        " 'PingFang SC', system-ui, sans-serif\" font-size=\"44\" font-weight=\"700\""
        ' letter-spacing="-1.2" fill="currentColor">Simple'
        f'<tspan fill="{STOPS[1][1]}">URL</tspan></text>\n'
        "</svg>\n"
    )


def render(svg: str, size: int, ss: int = 8) -> Image.Image:
    """超采样后缩回。cairosvg 在小尺寸下会自己改采样策略，8× 后再 LANCZOS
    缩回去才和 512px 版是同一个造型。"""
    import cairosvg

    png = cairosvg.svg2png(
        bytestring=svg.encode("utf-8"), output_width=size * ss, output_height=size * ss
    )
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    return im.resize((size, size), Image.LANCZOS)


def render_pngs() -> list[str]:
    icon = make_icon_svg()
    names: list[str] = []
    for size in PNG_SIZES:
        name = f"icon-{size}.png"
        render(icon, size).save(OUT / name)
        names.append(name)

    # 单色版也出两张，README / 深色底文档直接用
    mono = make_icon_svg(mono=True)
    for size in (48, 256):
        name = f"icon-mono-{size}.png"
        render(mono, size).save(OUT / name)
        names.append(name)

    maskable = make_icon_svg(scale=1 - 2 * MASKABLE_SAFE)
    render(maskable, 512).save(OUT / "icon-maskable.png")
    names.append("icon-maskable.png")

    # .ico 只放 16/32/48；每个尺寸各自超采样，不靠 ICO 自带的缩放
    ico = [render(icon, s) for s in (16, 32, 48)]
    ico[0].save(
        OUT.parent / "favicon.ico",
        format="ICO",
        sizes=[(i.width, i.height) for i in ico],
        append_images=ico[1:],
    )
    names.append("../favicon.ico")
    return names


def preview() -> str:
    """深底 / 浅底 / 各尺寸并排，改完一眼就能看出哪档糊了。"""
    sizes = (16, 20, 24, 32, 48, 64, 128)
    cells = []
    for bg, label in (("#ffffff", "浅底"), ("#0b1020", "深底")):
        items = "".join(
            f'<figure><img src="icon-{s}.png" width="{s}" height="{s}" alt="{s}px">'
            f"<figcaption>{s}</figcaption></figure>"
            for s in sizes
        )
        items += (
            '<figure><img src="icon.svg" width="96" height="96" alt="svg">'
            "<figcaption>svg</figcaption></figure>"
        )
        # 单独带一份 color，单色版靠 currentColor 上色，跟着标题色走会看不出效果
        cells.append(
            f'<section style="background:{bg};color:{"#111" if bg == "#ffffff" else "#fff"}">'
            f"<h2>{label}</h2><div class='row'>{items}</div></section>"
        )
    mono = "".join(
        f'<img src="icon-mono.svg" width="{s}" height="{s}" alt="mono {s}">'
        for s in (16, 24, 32, 64)
    )
    return f"""<!doctype html><meta charset="utf-8"><title>SimpleURL Logo 自查</title>
<style>
 body{{margin:0;font:14px/1.5 system-ui,sans-serif}}
 section{{padding:24px}} h2{{margin:0 0 16px;font-size:13px;letter-spacing:.12em;text-transform:uppercase;opacity:.6}}
 .row{{display:flex;align-items:flex-end;gap:22px;flex-wrap:wrap}}
 figure{{margin:0;text-align:center}} figcaption{{font-size:11px;opacity:.55;margin-top:6px}}
 .mono{{background:#f3f4f8;color:#1b2233;padding:24px;display:flex;gap:22px;align-items:center}}
 .mono.dark{{background:#0b1020;color:#eef2ff}}
</style>
{cells[0]}{cells[1]}
<div class="mono"><strong>单色版</strong>{mono}</div>
<div class="mono dark"><strong>单色版 · 深底</strong>{mono}</div>
"""


def main() -> None:
    (OUT / "icon.svg").write_text(make_icon_svg(), encoding="utf-8")
    (OUT / "favicon.svg").write_text(make_icon_svg(), encoding="utf-8")
    (OUT / "icon-mono.svg").write_text(make_icon_svg(mono=True), encoding="utf-8")
    (OUT / "icon-maskable.svg").write_text(
        make_icon_svg(scale=1 - 2 * MASKABLE_SAFE), encoding="utf-8"
    )
    (OUT / "logo-horizontal.svg").write_text(make_wordmark_svg(), encoding="utf-8")
    (OUT / "preview.html").write_text(preview(), encoding="utf-8")
    print("生成:", ", ".join(render_pngs()))


if __name__ == "__main__":
    main()
