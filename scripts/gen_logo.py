"""生成 SimpleURL 的 Logo：一枚 S 形的折返，没有底板。

造型就是一个字母 **S**：上道向右、右端折下、回程向左、左端再折下、下道向右。
下道的尽头是箭头，指向右 —— 长链接绕了一圈被折短，但去路仍然朝前。

这一版针对上一版真正的病重做（透明、尺寸一致性那些技术指标上一版都过了，
问题出在「读出来是什么」）：

1. **读成 Z** —— 上一版的两道横臂是等粗带子、末端各自切了个尖角，两块尖角
   像两个挂件，中间一根斜腰把它们连起来，整枚图形读作 Z，不是 S。
2. **尖角是长出来的零件** —— 箭头本该是笔画自己的收尾，上一版却在两端各切
   一刀，等于凭空多出两个三角，缩到 32px 就成了两个脏点。
3. **配色过艳** —— 青→紫→品红的高饱和渐变，压在小尺寸上色带会打架。
   这版收成「蓝→靛→紫」一档，同色系过渡，小图也不会花。

这一版的做法：

- **只有一处箭头**，而且长在下道的末端 —— 它是这条线的收尾，不是贴上去的装饰。
- **S 靠「两道半圆」立住**：上道右端接一个向下的半圆落到回程，回程左端接一个
  向下的半圆落到下道。两个半圆的半径由外框算出，不是手调贝塞尔。
  半径算出来的好处是：不管缩到多少像素，两道的间隙都按比例留着，不会糊成一块。
- **全尺寸同一套几何**：PNG 一律从同一份矢量 8 倍超采样再 LANCZOS 缩回，
  不按尺寸改线宽。16px 和 1024px 是同一条线的两个密度。

四条硬要求（每条都对应真栽过的坑）：

1. **真透明**：画布上只有笔画，最外圈像素一律清空。
2. **所有尺寸是同一张画**：同一份矢量 + 同一套缩放流程。
3. **画布由包围盒算出**：留白是算的，图形居中、永不贴边。
4. **不做像素级微调**：小图标要的是干净，不是补面积。

单色版必须给确定颜色：`currentColor` 在 cairosvg 里没有继承源，出图是全黑，
贴深底等于没有图标。所以单色 PNG 出两份（墨色 / 反白）。

依赖 cairosvg（只在出图时需要）：`pip install cairosvg`。

输出（app/static/logo/）：
- icon.svg / icon-{16..1024}.png   主图标
- favicon.svg / ../favicon.ico     站点图标
- icon-mono.svg / .png / -light    单色版（浅底 / 深底各一份）
- icon-maskable.svg / .png         PWA 用，带安全区
- logo-horizontal.svg              图标 + 字标横版
- preview.html                     浅底 / 深底 / 各尺寸 / 蒙版，一页自查
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

OUT = Path(__file__).resolve().parents[1] / "app" / "static" / "logo"
OUT.mkdir(parents=True, exist_ok=True)

# 品牌渐变：蓝 → 靛 → 紫，同色系一档过渡。方向从右下往上，横道收在浅端。
BLUE = "#3b82f6"
INDIGO = "#6366f1"
VIOLET = "#8b5cf6"

# ---------------------------------------------------------------- 造型
# 画布 100×100，图形居中，外框 46 见方。坐标写成算式不写成魔数：
# 改一个数（比如笔画粗细），所有圆弧和端点跟着重算，不会错位。
CX = CY = 50.0
SPAN = 46.0          # 图形外框边长

# 两套笔画参数，共用同一副骨架。这不是「按尺寸改线宽」那种把造型改掉的做法：
# 几何骨架完全一样，只是 16~24px 下笔画粗一档、箭头拉高，留白去掉，
# 免得缩到标签栏尺寸时箭头只剩一个点。
BIG = {"w": 7.2, "head": 22.0, "head_w": 11.0}
SMALL = {"w": 7.6, "head": 30.0, "head_w": 11.0}
SMALL_MAX = 24       # <= 此尺寸用 SMALL 那套参数 + 紧凑画布


def geom(p: dict[str, float]) -> dict[str, float]:
    """由笔画参数算出全部派生坐标。"""
    w = p["w"]
    rc = (SPAN - w) / 2
    return {
        "W": w,
        "RC": rc,
        "X_L": CX - SPAN / 2 + w / 2,
        "X_R": CX + SPAN / 2 - w / 2,
        "Y_T": CY - SPAN / 2 + w / 2,
        "Y_B": CY + SPAN / 2 - w / 2,
        "Y_M": CY,
        "C1Y": CY - rc,
        "C2Y": CY + rc,
        "HEAD": p["head"],
        "HEAD_W": p["head_w"],
    }


def _arc(cx: float, cy: float, r: float, sweep: int, upto: tuple[float, float]) -> str:
    """沿半圆走到 upto。半径和端点都由几何算出来，不手写角度。"""
    return f" A{r:.2f} {r:.2f} 0 0 {sweep} {upto[0]:.2f} {upto[1]:.2f}"


def s_path(g: dict[str, float]) -> str:
    """一笔到底：上道 → 右折 → 回程 → 左折 → 下道。"""
    return (
        f"M{g['X_L']:.2f} {g['Y_T']:.2f} H{g['X_R']:.2f}"
        + _arc(g["X_R"], g["C1Y"], g["RC"], 1, (g["X_R"], g["Y_M"]))
        + f" H{g['X_L']:.2f}"
        + _arc(g["X_L"], g["C2Y"], g["RC"], 0, (g["X_L"], g["Y_B"]))
        + f" H{g['X_R'] - g['HEAD_W'] + 2.0:.2f}"
    )


def arrow_path(g: dict[str, float]) -> str:
    """箭头：下道自己收成三角，指向右。不是贴上去的装饰。"""
    bx = g["X_R"] - g["HEAD_W"]
    return (
        f"M{bx:.2f} {g['Y_B'] - g['HEAD'] / 2:.2f} L{bx:.2f} {g['Y_B'] + g['HEAD'] / 2:.2f}"
        f" L{g['X_R'] + 0.5:.2f} {g['Y_B']:.2f} Z"
    )


def ink_box(g: dict[str, float]) -> tuple[float, float, float, float]:
    """笔画的真实包围盒。极值都在端点与箭头尖上，算出来而不是手填。"""
    return (g["X_L"] - g["W"] / 2, g["Y_T"] - g["W"] / 2,
            g["X_R"] + g["W"] / 2, g["Y_B"] + g["W"] / 2)


BIG_G = geom(BIG)
SMALL_G = geom(SMALL)

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


def content_box(g: dict[str, float], tight: bool = False) -> tuple[float, float, float, float]:
    """正方形画布，把整枚图形兜在正中。

    tight=True 时用更小的留白（小尺寸用）：图形占住大半格，标签栏上才看得清。
    留一点点边是必要的——贴到画布边就是深色页面上的一个方块。
    """
    x0, y0, x1, y1 = ink_box(g)
    span = max(x1 - x0, y1 - y0)
    pad = span * (TIGHT_BREATH if tight else BREATH)
    side = span + 2 * pad
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return (cx - side / 2, cy - side / 2, side, side)


def viewbox(g: dict[str, float], scale: float = 1.0, tight: bool = False) -> str:
    """scale < 1 时把图形缩进去，四周留安全区（maskable 用）。"""
    x, y, w, h = content_box(g, tight)
    if scale != 1.0:
        cx, cy = x + w / 2, y + h / 2
        w, h = w / scale, h / scale
        x, y = cx - w / 2, cy - h / 2
    return f"{x:.2f} {y:.2f} {w:.2f} {h:.2f}"


def _grad() -> str:
    # userSpaceOnUse：渐变跟着图形走，不跟画布走。否则换留白就会让颜色整体漂移。
    return (
        '<linearGradient id="g" x1="0" y1="1" x2="1" y2="0" gradientUnits="objectBoundingBox">'
        f'<stop offset="0" stop-color="{VIOLET}"/>'
        f'<stop offset=".55" stop-color="{INDIGO}"/>'
        f'<stop offset="1" stop-color="{BLUE}"/>'
        "</linearGradient>"
    )


def body(fill: str, g: dict[str, float] | None = None) -> str:
    g = g or BIG_G
    return (
        f'<path d="{s_path(g)}" fill="none" stroke="{fill}" stroke-width="{g["W"]:g}"'
        ' stroke-linecap="round" stroke-linejoin="round"/>'
        f'<path d="{arrow_path(g)}" fill="{fill}"/>'
    )


# 小尺寸单独一套笔画参数。不是「按尺寸改线宽」那种把造型改掉的做法：
# 几何骨架完全一样，只是 16px 下留白被去掉、笔画相对粗一档、箭头拉高，
# 免得缩到标签栏尺寸时箭头只剩一个点。
SMALL_W = 8.8
SMALL_HEAD = 30.0
SMALL_HEAD_W = 14.0


def _geom(w: float, head: float, head_w: float) -> dict[str, float]:
    """给定笔画参数，重算所有派生坐标。两套参数共用同一套骨架。"""
    rc = (SPAN - w) / 2
    return {
        "W": w,
        "RC": rc,
        "X_L": CX - SPAN / 2 + w / 2,
        "X_R": CX + SPAN / 2 - w / 2,
        "Y_T": CY - SPAN / 2 + w / 2,
        "Y_B": CY + SPAN / 2 - w / 2,
        "C1Y": CY - rc,
        "C2Y": CY + rc,
        "HEAD": head,
        "HEAD_W": head_w,
    }


def make_icon_svg(
    scale: float = 1.0,
    mono: bool = False,
    size: int = 100,
    ink: str = "currentColor",
    g: dict[str, float] | None = None,
) -> str:
    paint = ink if mono else "url(#g)"
    defs = "" if mono else f"<defs>{_grad()}</defs>"
    g = g or BIG_G
    return (
        '<svg xmlns="http://www.w3.org/2000/svg"'
        f' viewBox="{viewbox(g, scale)}" width="{size}" height="{size}"'
        ' role="img" aria-label="SimpleURL">'
        f"{defs}{body(paint, g)}"
        "</svg>\n"
    )


WORDMARK = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 392 100" width="392" height="100"'
    ' role="img" aria-label="SimpleURL">\n'
    "  <defs>{grad}</defs>\n"
    '  <g transform="translate(2 0) scale({scale:.4f}) translate({tx:.2f} {ty:.2f})">{body_svg}</g>\n'
    '  <text x="102" y="66" font-family="Inter, \'SF Pro Display\', -apple-system,'
    " 'Segoe UI', 'PingFang SC', system-ui, sans-serif\" font-size=\"46\""
    ' font-weight="700" letter-spacing="-1.4" fill="{ink}">Simple'
    '<tspan fill="{accent}">URL</tspan></text>\n'
    "</svg>\n"
)


def make_wordmark_svg(dark: bool = False) -> str:
    """横版：图标 + 字标。

    字标颜色必须焊死在文件里，不能用 currentColor：
    这个 SVG 通常是被 <img src> 引用的，而 <img> 里的 SVG 拿不到页面的
    color —— currentColor 会落到初始值黑色，深底上就是一团看不见的字。
    （上一版就是栽在这里，自查页里看得清清楚楚。）
    所以出两份，用 prefers-color-scheme 决定用哪份：直接内联进页面时
    它会跟着系统主题走；作为 <img> 引用时也仍然是明确的黑或白。

    图标按图形包围盒贴进方格——按 viewBox 直接摆会带着两侧留白，
    看起来像图标和文字之间凭空多了一格。
    """
    x0, y0, x1, y1 = ink_box(BIG_G)
    side = 80
    k = side / max(x1 - x0, y1 - y0)
    return WORDMARK.format(
        body_svg=body("url(#g)", BIG_G),
        grad=_grad(),
        scale=k,
        tx=6 - x0,
        ty=(100 - (y1 - y0) * k) / 2 - y0,
        accent=INDIGO,
        ink="#eef2ff" if dark else MONO_INK,
    )


def make_wordmark_themed_svg() -> str:
    """默认那份：给「字已经配深色」的场景用，就是深墨色。

    为什么不在这里写 prefers-color-scheme：SVG 被 <img> 引用时，
    媒体查询由**渲染器**决定，而光栅化器（cairosvg 之类）根本不认这条规则，
    最终会静默落到浅色那一版。与其赌环境，不如把两版拆成两个文件，
    让人明确挑一份——见 logo-horizontal-dark.svg。
    """
    return make_wordmark_svg(dark=False)


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
        g = SMALL_G if size <= SMALL_MAX else BIG_G
        svg = make_icon_svg(g=g)
        name = f"icon-{size}.png"
        scrub_edges(render(svg, size)).save(OUT / name, optimize=True)
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
    ico = [scrub_edges(render(make_icon_svg(g=SMALL_G if s <= SMALL_MAX else BIG_G), s))
           for s in (16, 32, 48)]
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
    (OUT / "favicon.svg").write_text(make_icon_svg(size=64), encoding="utf-8")
    (OUT / "icon-mono.svg").write_text(make_icon_svg(mono=True, scale=0.96), encoding="utf-8")
    (OUT / "icon-maskable.svg").write_text(
        make_icon_svg(scale=1 - 2 * MASKABLE_SAFE), encoding="utf-8"
    )
    (OUT / "logo-horizontal.svg").write_text(make_wordmark_themed_svg(), encoding="utf-8")
    # 深底专用的一份：字标用浅色。拆成两个文件而不是靠媒体查询，
    # 是因为 <img> 引 SVG 时媒体查询的求值环境不可控（见 make_wordmark_themed_svg）。
    (OUT / "logo-horizontal-dark.svg").write_text(make_wordmark_svg(dark=True), encoding="utf-8")
    (OUT / "preview.html").write_text(preview_html(), encoding="utf-8")
    print("生成:", ", ".join(render_pngs()))


if __name__ == "__main__":
    main()
