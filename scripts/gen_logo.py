"""生成 SimpleURL 的 Logo：一枚「折返箭头」，纯透明背景，无底板。

造型（用户点名的原设计，别改）：一条折返线从右上绕回，把去路折成一个箭头
—— 长链接在这里被绕短，箭头指出它通向哪儿。
渐变色 `#4f8cff → #7b5cff → #d946ef` 沿对角铺开。

## 这一版的来历，写下来免得再走一遍

第 1 版就是这个造型。之后我连着「重做」了三版（S 骨架 / 两道折臂 / 一笔折返），
每一版都在 Issue 里论证得很像那么回事，用户每次都说「上面的那个，
我要图片里上面那个」。

病根：我把「重做 Logo」理解成「设计一个更好的图形」，拿测试全绿当验收。
那批测试量的是透明、尺寸一致、不贴边、16px 空腔比例 —— 技术指标全过，
图形可以完全是另一枚。**规格应该是用户指的那张图，不是我的测试。**
所以这一版把原设计的几何直接写死，测试钉的也是「这枚图形」本身。

## 造型要点

- **一笔不断**：横臂 → 右端圆弯 → 回程下折 → 箭头，同一根笔画走完，
  中间没有拼接，渐变在拐弯处连得上。
- **箭头是笔的收尾**：不是贴上去的三角，是两个描边的端点，方向由笔势定。
- **真透明**：画布上只有笔画，最外圈像素一律清空，深色页面上不会有毛边方块。
- **所有尺寸同一张画**：同一份矢量 8 倍超采样再 LANCZOS 缩回。
- **单色版给确定颜色**：`currentColor` 在 cairosvg 里没有继承源，出图全黑，
  贴深底等于没有图标。所以单色 PNG 出两份（墨色 / 反白）。

依赖 cairosvg（只在出图时需要）：`pip install cairosvg`。

输出（app/static/logo/）：
- icon.svg / icon-{16..1024}.png   主图标
- favicon.svg / ../favicon.ico     站点图标
- icon-mono.svg / .png / -light    单色版（浅底 / 深底各一份）
- icon-maskable.svg / .png         PWA 用，带安全区
- logo-horizontal{,-dark}.svg      图标 + 字标横版（深底 / 浅底各一份）
- preview.html                     浅底 / 深底 / 各尺寸 / 蒙版，一页自查
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

OUT = Path(__file__).resolve().parents[1] / "app" / "static" / "logo"
OUT.mkdir(parents=True, exist_ok=True)

# 品牌渐变：蓝 → 紫 → 品红，沿对角线铺开。这就是原设计的三个色值。
BLUE = "#4f8cff"
PURPLE = "#7b5cff"
PINK = "#d946ef"

# ---------------------------------------------------------------- 造型
# 坐标固定在 96×96 画布上（原设计的坐标系，图形实际占用 x 18..80, y 24..82）。
# 三笔全部是描边路径，圆头圆角：
#   1. 横臂 + 右端半圆 + 回程下折，一笔到底
#   2. 箭头的两条斜边（从回程末端折出来）
BODY_MAIN = "M22 30h26a16 16 0 0 1 0 32H46"
BODY_HEAD = "M36 48 24 62 36 76"
VIEW_TIGHT = "6 14 84 76"   # favicon 用：只取图形，去掉多余留白
CANVAS = 96

# 描边随渲染尺寸微调：小图加粗一档，免得标签栏上糊成一团。
# 注意这是「同一造型的不同密度」，不是换一枚图标 —— 骨架完全一样。
STROKE_STEPS = ((16, 13.5), (32, 12.5), (48, 12), (96, 11), (10**9, 11))


def stroke_for(size: int) -> float:
    for limit, width in STROKE_STEPS:
        if size <= limit:
            return width
    return 11


def ink_box() -> tuple[float, float, float, float]:
    """笔画的真实包围盒：路径极值 + 描边外沿。

    极值都在端点和圆弧顶点上（x 18..80, y 24..82），再加半个线宽。
    手写常量而不是解析路径：这三个端点就摆在这儿，解析反而更容易错。
    """
    half = STROKE_STEPS[-1][1] / 2
    return 18.5 - half, 24 - half, 79.5 + half, 82 + half


def ink_span() -> float:
    """图形长边（正方形画布的图形边长）。"""
    x0, y0, x1, y1 = ink_box()
    return max(x1 - x0, y1 - y0)

BREATH = 0.32        # 四周留白占图形长边的比例：图形约占画布 61%，四角留出空气
TIGHT_BREATH = 0.10  # 小尺寸微调留白

PNG_SIZES = (16, 20, 24, 32, 48, 64, 96, 128, 180, 192, 256, 512, 1024)
# 小尺寸的画布：和其余尺寸同一副骨架、同样的留白比例，只换笔画参数。
# 早先给小尺寸单独收窄画布（让图形占满整格），结果是同一枚图标在
# 16px 下放大一圈，和 32px 并排看就不是同一张画了。
MONO_SIZES = (16, 24, 32, 48, 64, 256)
MONO_INK = "#1b2233"
MONO_LIGHT = "#ffffff"
MASKABLE_SAFE = 0.2
SUPERSAMPLE = 8

BREATH = 0.14   # 四周留白占图形长边的比例（原设计的留白手感）

def content_box(scale: float = 1.0) -> tuple[float, float, float, float]:
    """正方形画布，把整枚图形兜在正中。

    scale < 1 时把画布撑大、图形相对缩小，四周留出安全区（maskable / 单色版用）。
    """
    x0, y0, x1, y1 = ink_box()
    span = max(x1 - x0, y1 - y0)
    pad = span * BREATH
    side = (span + 2 * pad) / scale
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return (cx - side / 2, cy - side / 2, side, side)


def viewbox(scale: float = 1.0) -> str:
    x, y, w, h = content_box(scale)
    return f"{x:.2f} {y:.2f} {w:.2f} {h:.2f}"


def _grad() -> str:
    # objectBoundingBox：渐变跟着图形走，不跟画布走。
    # 换留白比例时颜色不会整体漂移。
    return (
        '<linearGradient id="gMain" x1="0" y1="1" x2="1" y2="0">'
        f'<stop offset="0" stop-color="{BLUE}"/>'
        f'<stop offset=".55" stop-color="{PURPLE}"/>'
        f'<stop offset="1" stop-color="{PINK}"/>'
        "</linearGradient>"
    )


def body(fill: str, stroke: float = 11) -> str:
    """两笔：主体的折返线 + 箭头的两条斜边。都是描边，圆头圆角。"""
    return "".join(
        f'<path d="{d}" fill="none" stroke="{fill}" stroke-width="{stroke:g}"'
        ' stroke-linecap="round" stroke-linejoin="round"/>'
        for d in (BODY_MAIN, BODY_HEAD)
    )


def make_icon_svg(
    scale: float = 1.0,
    mono: bool = False,
    size: int = 96,
    ink: str = "currentColor",
    stroke: float = 11,
) -> str:
    paint = ink if mono else "url(#gMain)"
    defs = "" if mono else f"<defs>{_grad()}</defs>"
    return (
        '<svg xmlns="http://www.w3.org/2000/svg"'
        f' viewBox="{viewbox(scale)}" width="{size}" height="{size}"'
        ' role="img" aria-label="SimpleURL">'
        f"{defs}{body(paint, stroke)}"
        "</svg>\n"
    )


def make_favicon_svg(stroke: float = 12) -> str:
    """站点图标：画布收紧到图形本身，标签栏上更醒目。"""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{VIEW_TIGHT}"'
        ' width="96" height="87" role="img" aria-label="SimpleURL">'
        f"<defs>{_grad()}</defs>{body('url(#gMain)', stroke)}"
        "</svg>\n"
    )


WORDMARK = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 392 100" width="392" height="100"'
    ' role="img" aria-label="SimpleURL">\n'
    "  <defs>{grad}</defs>\n"
    '  <g transform="translate({tx:.2f} {ty:.2f}) scale({scale:.4f})">{body_svg}</g>\n'
    '  <text x="104" y="66" font-family="Inter, \'SF Pro Display\', -apple-system,'
    " 'Segoe UI', 'PingFang SC', system-ui, sans-serif\" font-size=\"46\""
    ' font-weight="700" letter-spacing="-1.4" fill="{ink}">Simple'
    '<tspan fill="{accent}">URL</tspan></text>\n'
    "</svg>\n"
)


def make_wordmark_svg(dark: bool = False) -> str:
    """横版：图标 + 字标。

    字标颜色焊死在文件里，不用 currentColor：这个 SVG 通常被 <img src> 引用，
    而 <img> 里的 SVG 拿不到页面的 color，currentColor 会落到初始值黑色，
    深底上整行字看不见。所以出两份：默认墨色，`-dark` 反白。

    图标按包围盒贴进 84×84 的方格——直接按 viewBox 摆会带着两侧留白，
    看起来像图标和文字之间凭空多了一格。
    """
    x0, y0, x1, y1 = ink_box()
    k = 84 / max(x1 - x0, y1 - y0)
    return WORDMARK.format(
        body_svg=body("url(#gMain)", 11),
        grad=_grad(),
        scale=k,
        tx=2 - x0 * k,
        ty=(100 - (y1 - y0) * k) / 2 - y0 * k,
        accent=PURPLE,
        ink="#eef2ff" if dark else MONO_INK,
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
    names: list[str] = []
    for size in PNG_SIZES:
        name = f"icon-{size}.png"
        svg = make_icon_svg(stroke=stroke_for(size))
        scrub_edges(render(svg, size)).save(OUT / name, optimize=True)
        names.append(name)

    # 单色两份：墨色配浅底，反白配深底。颜色焊死在文件里，不靠 currentColor。
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
    ico = [
        scrub_edges(render(make_icon_svg(stroke=stroke_for(s) + 1), s))
        for s in (16, 32, 48)
    ]
    ico[0].save(
        OUT.parent / "favicon.ico",
        format="ICO",
        sizes=[(i.width, i.height) for i in ico],
        append_images=ico[1:],
    )
    names.append("../favicon.ico")
    return names


def _is_dark(hexcolor: str) -> bool:
    """这点颜色放上去算不算深底（相对亮度 < 0.5）。"""
    h = hexcolor.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 < 0.5


def preview_html() -> str:
    """一页看全：真实像素 vs 放大、浅底 / 深底 / 各尺寸 / 蒙版 / 单色。"""
    sizes = (16, 24, 32, 48, 64, 96, 128, 192, 512)

    # 小尺寸单开一段：左边真实像素、右边放大 8 倍。看骨架，不看虚化。
    zoom = "".join(
        f'<figure><div class="pair"><img src="icon-{s}.png" width="{s}" height="{s}" alt="{s}px">'
        f'<img class="big" src="icon-{s}.png" width="{s * 6}" height="{s * 6}" alt="放大">'
        f"</div><figcaption>{s}px</figcaption></figure>"
        for s in (16, 24, 32, 48)
    )

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
        # <img> 引 SVG 拿不到页面 color：字标和单色版都必须用「颜色焊死在文件里」的那份
        # 判断「深底还是浅底」按亮度算，别按背景色的字面值比：
        # 早先写成 bg == "#ffffff"，加了一档浅灰底之后那一段就悄悄用上了反白字标。
        mark = "logo-horizontal-dark.svg" if _is_dark(bg) else "logo-horizontal.svg"
        items += (
            f'<figure style="background:{bg}">'
            f'<img src="{mark}" width="224" height="57" alt="字标">'
            f"<figcaption>字标</figcaption></figure>"
        )
        rows.append(
            f'<section style="background:{bg};color:{fg}">'
            f"<h2>{label}</h2><div class='row'>{items}</div></section>"
        )

    # 单色版直接摆 PNG，不摆 SVG：`<img>` 里的 SVG 不继承页面 color，
    # currentColor 会落到黑色，深底那一段整片看不见。自查页自己出错，
    # 比图标出错更难发现——所以只用「颜色已经焊死在文件里」的那份。
    # 墨色那份配浅底，反白那份配深底。名字容易绕：icon-mono-*.png 是墨色，
    # icon-mono-light-*.png 是反白（给深底用的）—— 别再把反白摆到白纸上。
    mono_ink_row = "".join(
        f'<img src="icon-mono-{s}.png" width="{s}" height="{s}" alt="mono {s}">' for s in (16, 24, 32, 64)
    )
    mono_light_row = "".join(
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
 .pair{{display:flex;align-items:flex-end;gap:10px}} .big{{image-rendering:pixelated;opacity:.9}}
 .mono{{background:#f3f4f8;color:#1b2233;padding:24px;display:flex;gap:22px;align-items:center}}
 .mono.dark{{background:#0b1020;color:#ffffff}}
 .mono img{{display:block}}
 .check{{display:flex;align-items:center;gap:10px;padding:0 24px 24px;font-size:13px;opacity:.75}}
</style>
<section style="background:#fff;color:#111"><h2>小尺寸实测 · 左真实像素，右放大 6 倍</h2><div class="row">{zoom}</div></section>
{rows[0]}
{rows[1]}
<section style="background:#0b1020;color:#fff"><h2>PWA 蒙版裁切（内容须落在中间 80%）</h2><div class="row">{mask}</div></section>
{rows[2]}
<div class="mono"><strong>单色版 · 墨色（配浅底）</strong>{mono_ink_row}</div>
<div class="mono dark"><strong>单色版 · 反白（配深底）</strong>{mono_light_row}</div>
<p class="check">四角必须完全透明：底色变来变去，图形边缘都不该出现任何方块。</p>
"""


def main() -> None:
    (OUT / "icon.svg").write_text(make_icon_svg(), encoding="utf-8")
    (OUT / "favicon.svg").write_text(make_favicon_svg(stroke_for(48) + 1), encoding="utf-8")
    (OUT / "icon-mono.svg").write_text(make_icon_svg(mono=True, scale=0.96), encoding="utf-8")
    (OUT / "icon-maskable.svg").write_text(
        make_icon_svg(scale=1 - 2 * MASKABLE_SAFE), encoding="utf-8"
    )
    (OUT / "logo-horizontal.svg").write_text(make_wordmark_svg(), encoding="utf-8")
    # 深底专用的一份：字标反白。拆成两个文件而不是靠媒体查询，
    # 是因为 <img> 引 SVG 时媒体查询的求值环境不可控。
    (OUT / "logo-horizontal-dark.svg").write_text(make_wordmark_svg(dark=True), encoding="utf-8")
    (OUT / "preview.html").write_text(preview_html(), encoding="utf-8")
    print("生成:", ", ".join(render_pngs()))


if __name__ == "__main__":
    main()
