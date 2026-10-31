"""生成 SimpleURL 的 Logo：一个字母 S，一笔画完，没有底板。

造型：一条折返的笔画走成小写「s」——上半弯勾住下半弯，收口处折回来，
末端伸出一个小箭头指向内侧。字面是 S，动作是「长链接被折短」。

四条硬要求，每一条都对应真栽过的坑：

1. **真透明**：画布上只有这一笔，最外圈像素一律清空。
2. **所有尺寸是同一张画**：PNG 全部从同一条路径 8 倍超采样再 LANCZOS 缩回。
   按尺寸分档改描边，16px 和高清版就成了两枚图标。
3. **画布由包围盒算出**：留白是算的，不是手填 viewBox，图形永远居中不贴边。
4. **不做像素级微调**：小图标要的是干净，不是补面积。描边一旦加粗、
   笔画之间的缝就没了，16px 会糊成一块。

单色版必须给出确定颜色：`currentColor` 在 cairosvg 里没有继承源，出图是全黑，
贴深底等于没有图标。所以单色 PNG 出两份（墨色 / 反白）。

依赖 cairosvg（只在出图时需要）：`pip install cairosvg`。

输出（app/static/logo/）：
- icon.svg / icon-{16..1024}.png   主图标
- favicon.svg / ../favicon.ico     站点图标
- icon-mono.svg / .png / -light    单色版（浅底 / 深底各一份）
- icon-maskable.svg / .png         PWA 用，带 20% 安全区
- logo-horizontal.svg              图标 + 字标横版
- preview.html                     浅底 / 深底 / 各尺寸 / 蒙版，一页自查
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from PIL import Image

OUT = Path(__file__).resolve().parents[1] / "app" / "static" / "logo"
OUT.mkdir(parents=True, exist_ok=True)

# 品牌渐变：青 → 紫 → 品红。方向顺着笔画走，收口处才由紫转粉。
STOPS = ((0.0, "#35d0ff"), (0.52, "#7c6cff"), (1.0, "#ff5ac8"))

# 一笔 s：上弯 → 中横 → 下弯，末端略微上挑（收口）
BODY = (
    "M62 34C56 24 44 18 34 21C22 24 14 34 14 45"
    "C14 57 24 61 36 61C48 61 60 65 60 77"
    "C60 91 48 99 34 96C26 94 20 90 15 85"
)
# 收口处的箭头，指回笔画内侧（左上）。实心、跟着渐变走，和笔画同一枚图形。
ARROW = "M15 85L0 83L8 102Z"
STROKE = 14

PNG_SIZES = (16, 20, 24, 32, 48, 64, 96, 128, 180, 192, 256, 512, 1024)
MONO_SIZES = (16, 24, 32, 48, 64, 256)
MONO_INK = "#1b2233"
MONO_LIGHT = "#ffffff"
MASKABLE_SAFE = 0.2
SUPERSAMPLE = 8

# 图形包围盒：路径端点 + 箭头 + 描边外沿，再留出呼吸。
# 用固定值而不是解析路径——这条路径的极值点都在端点上，手填反而不会错。
INK = (2.0, 14.5, 77.0, 105.5)   # x0, y0, x1, y1（含线宽与箭头）
BREATH = 0.24                     # 四周留白占图形长边的比例。0.14 时 16px 只剩 0.8px 空隙，抗锯齿一糊就贴边；留够才算「不贴边」


def content_box() -> tuple[float, float, float, float]:
    """正方形画布，把整枚图形兜在正中。"""
    x0, y0, x1, y1 = INK
    span = max(x1 - x0, y1 - y0)
    pad = span * BREATH
    side = span + 2 * pad
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return (cx - side / 2, cy - side / 2, side, side)


def viewbox(scale: float = 1.0) -> str:
    """scale < 1 时把图形缩进去，四周留安全区（maskable 用）。"""
    x, y, w, h = content_box()
    if scale != 1.0:
        cx, cy = x + w / 2, y + h / 2
        w, h = w / scale, h / scale
        x, y = cx - w / 2, cy - h / 2
    return f"{x:.2f} {y:.2f} {w:.2f} {h:.2f}"


def _grad() -> str:
    stops = "".join(f'<stop offset="{o:g}" stop-color="{c}"/>' for o, c in STOPS)
    # userSpaceOnUse：渐变跟着图形走，不跟画布走。否则改了留白，
    # 同一枚图标的颜色就会整体漂移。
    x0, y0, x1, y1 = 14, 20, 62, 100
    return (
        f'<linearGradient id="g" x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}"'
        f' gradientUnits="userSpaceOnUse">{stops}</linearGradient>'
    )


def body(fill: str) -> str:
    return (
        f'<path d="{BODY}" fill="none" stroke="{fill}" stroke-width="{STROKE}"'
        ' stroke-linecap="round"/>'
        f'<path d="{ARROW}" fill="{fill}"/>'
    )


def make_icon_svg(
    scale: float = 1.0,
    mono: bool = False,
    size: int = 100,
    ink: str = "currentColor",
) -> str:
    paint = ink if mono else "url(#g)"
    defs = "" if mono else f"<defs>{_grad()}</defs>"
    return (
        '<svg xmlns="http://www.w3.org/2000/svg"'
        f' viewBox="{viewbox(scale)}" width="{size}" height="{size}"'
        ' role="img" aria-label="SimpleURL">'
        f"{defs}{body(paint)}"
        "</svg>\n"
    )


WORDMARK = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 392 100" width="392" height="100"'
    ' role="img" aria-label="SimpleURL">\n'
    "  <defs>{grad}</defs>\n"
    '  <g transform="translate(2 0) scale({scale:.4f}) translate({tx:.2f} {ty:.2f})">{body_svg}</g>\n'
    '  <text x="102" y="66" font-family="Inter, \'SF Pro Display\', -apple-system,'
    " 'Segoe UI', 'PingFang SC', system-ui, sans-serif\" font-size=\"46\""
    ' font-weight="700" letter-spacing="-1.4" fill="currentColor">Simple'
    '<tspan fill="{accent}">URL</tspan></text>\n'
    "</svg>\n"
)


def make_wordmark_svg() -> str:
    """横版：图标 + 字标。

    字标用 currentColor，深浅底都跟着走；只有 URL 三个字母锁品牌紫。
    图标按图形包围盒贴进 88×88 的方格——按 viewBox 直接摆会带着两侧留白，
    看起来像图标和文字之间凭空多了一格。
    """
    x0, y0, x1, y1 = INK
    side = 80
    k = side / max(x1 - x0, y1 - y0)
    return WORDMARK.format(
        body_svg=body("url(#g)"),
        grad=_grad(),
        scale=k,
        tx=6 - x0,
        ty=(100 - (y1 - y0) * k) / 2 - y0,
        accent=STOPS[1][1],
    )


def render(svg: str, size: int, ss: int = SUPERSAMPLE) -> Image.Image:
    """超采样后缩回：小尺寸和高清版因此是同一张画。"""
    import cairosvg

    png = cairosvg.svg2png(
        bytestring=svg.encode("utf-8"), output_width=size * ss, output_height=size * ss
    )
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    return im.resize((size, size), Image.LANCZOS)


def scrub_edges(img: Image.Image) -> Image.Image:
    """把最外一圈像素清空。

    抗锯齿会在画布边缘漏出 alpha=1~4 的像素：肉眼看不见，但深色背景上
    这就是一圈毛边。图形离边至少 1 像素，切掉这一圈换不到任何可见损失。
    """
    w, h = img.size
    px = img.load()
    for x in range(w):
        px[x, 0] = (0, 0, 0, 0)
        px[x, h - 1] = (0, 0, 0, 0)
    for y in range(h):
        px[0, y] = (0, 0, 0, 0)
        px[w - 1, y] = (0, 0, 0, 0)
    return img


def render_pngs() -> list[str]:
    icon = make_icon_svg()
    names: list[str] = []
    for size in PNG_SIZES:
        name = f"icon-{size}.png"
        scrub_edges(render(icon, size)).save(OUT / name, optimize=True)
        names.append(name)

    for suffix, ink in (("", MONO_INK), ("-light", MONO_LIGHT)):
        mono = make_icon_svg(mono=True, scale=0.96, ink=ink)
        for size in MONO_SIZES:
            name = f"icon-mono{suffix}-{size}.png"
            scrub_edges(render(mono, size)).save(OUT / name, optimize=True)
            names.append(name)

    maskable = make_icon_svg(scale=1 - 2 * MASKABLE_SAFE)
    scrub_edges(render(maskable, 512)).save(OUT / "icon-maskable.png", optimize=True)
    names.append("icon-maskable.png")

    # .ico 只放 16/32/48：小尺寸靠超采样自己出，不交给 ICO 自带的缩放
    ico = [scrub_edges(render(icon, s)) for s in (16, 32, 48)]
    ico[0].save(
        OUT.parent / "favicon.ico",
        format="ICO",
        sizes=[(i.width, i.height) for i in ico],
        append_images=ico[1:],
    )
    names.append("../favicon.ico")
    return names


def preview_html() -> str:
    """一页看全：浅底 / 深底 / 各尺寸 / 蒙版裁切 / 单色。改完图先开这页。"""
    sizes = (16, 20, 24, 32, 48, 64, 128)
    rows = []
    for bg, fg, label in (
        ("#ffffff", "#111827", "浅底"),
        ("#0b1020", "#eef2ff", "深底"),
        ("#eef0f6", "#1e293b", "浅灰底"),
    ):
        items = "".join(
            f'<figure><img src="icon-{s}.png" width="{s}" height="{s}" alt="{s}px">'
            f"<figcaption>{s}</figcaption></figure>"
            for s in sizes
        )
        items += (
            '<figure><img src="icon.svg" width="96" height="96" alt="svg">'
            "<figcaption>svg</figcaption></figure>"
        )
        # 字标自己带框：同一个文件要能放进「浅底」和「深底」两段里各看一次
        items += (
            f'<figure style="background:{bg}">'
            f'<img src="logo-horizontal.svg" width="224" height="57" alt="字标"'
            f' style="color:{fg}"><figcaption>字标（Simple 跟 currentColor）</figcaption></figure>'
        )
        rows.append(
            f'<section style="background:{bg};color:{fg}">'
            f"<h2>{label}</h2><div class='row'>{items}</div></section>"
        )

    # 单色版直接摆 PNG，不摆 SVG：`<img>` 里的 SVG 不继承页面 color，
    # currentColor 会落到黑色，深底那一段整片看不见。自查页自己出错，
    # 比图标出错更难发现——所以自查页只用「颜色已经焊死在文件里」的那份。
    mono_light_row = "".join(
        f'<img src="icon-mono-{s}.png" width="{s}" height="{s}" alt="mono {s}">' for s in (16, 24, 32, 64)
    )
    mono_dark_row = "".join(
        f'<img src="icon-mono-light-{s}.png" width="{s}" height="{s}" alt="mono light {s}">'
        for s in (16, 24, 32, 64)
    )
    # maskable 会被系统裁成圆形 / 水滴，这里用 border-radius 直接模拟
    mask = "".join(
        f'<figure><img src="icon-maskable.png" width="96" height="96" alt="maskable"'
        f' style="border-radius:{r};background:#243049"><figcaption>{n}</figcaption></figure>'
        for r, n in (("50%", "圆形裁切"), ("28%", "圆角裁切"), ("0", "原图"))
    )
    return f"""<!doctype html><meta charset="utf-8"><title>SimpleURL Logo 自查</title>
<style>
 body{{margin:0;font:14px/1.5 system-ui,sans-serif;background:#fff;color:#111}}
 section{{padding:24px}} h2{{margin:0 0 16px;font-size:13px;letter-spacing:.12em;text-transform:uppercase;opacity:.6}}
 .row{{display:flex;align-items:flex-end;gap:22px;flex-wrap:wrap}}
 figure{{margin:0;text-align:center}} figcaption{{font-size:11px;opacity:.55;margin-top:6px}}
 .mono{{background:#f3f4f8;color:#1b2233;padding:24px;display:flex;gap:22px;align-items:center}}
 .mono.dark{{background:#0b1020;color:#ffffff}}
 .mono img{{display:block}}
 .check{{display:flex;align-items:center;gap:10px;padding:0 24px 24px;font-size:13px;opacity:.75}}
</style>
{rows[0]}
{rows[1]}
<section style="background:#0b1020;color:#fff"><h2>PWA 蒙版裁切（内容须落在中间 80%）</h2><div class="row">{mask}</div></section>
{rows[2]}
<div class="mono"><strong>单色版 · 浅底</strong>{mono_light_row}</div>
<div class="mono dark"><strong>单色版 · 深底</strong>{mono_dark_row}</div>
<p class="check">四角必须完全透明：底色变来变去，图形边缘都不该出现任何方块。</p>
"""


def main() -> None:
    (OUT / "icon.svg").write_text(make_icon_svg(), encoding="utf-8")
    (OUT / "favicon.svg").write_text(make_icon_svg(size=64), encoding="utf-8")
    (OUT / "icon-mono.svg").write_text(make_icon_svg(mono=True, scale=0.96), encoding="utf-8")
    (OUT / "icon-maskable.svg").write_text(
        make_icon_svg(scale=1 - 2 * MASKABLE_SAFE), encoding="utf-8"
    )
    (OUT / "logo-horizontal.svg").write_text(make_wordmark_svg(), encoding="utf-8")
    (OUT / "preview.html").write_text(preview_html(), encoding="utf-8")
    print("生成:", ", ".join(render_pngs()))


if __name__ == "__main__":
    main()
