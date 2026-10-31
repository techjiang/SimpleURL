"""生成 SimpleURL 的 Logo：两道折臂 + 一根斜腰，没有底板。

造型就是一个字母 **S**：上折（末端收尖，指右）、斜腰（横穿中心）、
下折（末端收尖，指左）。两条带子两条方向，中间那根腰把它们连成一条
不断的路——顺带说出了这个产品干的事：长链接被折短，还能指得回去。
整枚图形只有笔画本身，没有底板、没有圆角方块，贴什么底色都只剩这几笔。

这一版针对上一版真正的病重做（透明度、尺寸一致性那些技术指标上一版已过）：

1. **读成 C，不是 S** —— 上一版的下半环收口拐进了圆环内部，右下没有开口，
   整枚图形读起来是个 C。这版把 S 拆成三段结构量出来：折臂方向相反、
   斜腰必须横穿中心，S 才立得住。上一版是「一条曲线凭手感调」，
   调歪了就什么也不是。
2. **箭头是浮在笔画外的一块装饰** —— 上一版的箭头贴在外侧、还指反了，
   缩到 32px 就是一枚脏点。这版让折臂末端自己收成尖：箭头不是贴上去的，
   是这一笔的收尾，方向由笔势定。
3. **小尺寸下两道弯挤成一块** —— 折臂是横向等粗带子，16px 时上下两条
   仍然分得开。上一版 16px 里笔画之间只剩 1px 缝，一缩就糊。

四条硬要求（每条都对应真栽过的坑）：

1. **真透明**：画布上只有这几笔，最外圈像素一律清空。
2. **所有尺寸是同一张画**：PNG 全部从同一组路径 8 倍超采样再 LANCZOS 缩回。
   按尺寸分档改描边，16px 和高清版就成了两枚图标。
3. **画布由包围盒算出**：留白是算的，不是手填 viewBox，图形永远居中不贴边。
4. **不做像素级微调**：小图标要的是干净，不是补面积。

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
import math
import re
from pathlib import Path

from PIL import Image

OUT = Path(__file__).resolve().parents[1] / "app" / "static" / "logo"
OUT.mkdir(parents=True, exist_ok=True)

# 品牌渐变：青 → 紫 → 品红。方向顺着笔画走，收口处才由紫转粉。
STOPS = ((0.0, "#35d0ff"), (0.52, "#7c6cff"), (1.0, "#ff5ac8"))

# ---------------------------------------------------------------- 造型
# 「S」的两道折：两道水平笔画 + 中间一道斜腰。全部由端点算出来，
# 不手调贝塞尔控制点——上一版的腰就是凭感觉调歪的，歪了以后上下两弯
# 各自朝外倾，整枚图形读成了 C。
#
# 坐标约定：画布 100×100，y 轴朝下。上臂从左往右、下臂从右往左，
# 中间那根斜腰把它们连成一条不断的路——这就是「把长链接折短」的动作。
BAR = 13.0            # 两道折臂的粗细（纵向）
SLANT = 13.5          # 腰的粗细：和折臂的厚度对齐，三条带子才像一笔写完的
X0, X1 = 15.0, 85.0   # 左右端点
Y_TOP, Y_MID, Y_BOT = 24.0, 58.0, 85.0   # 上臂 / 腰中点 / 下臂 的中心线

def _bevel_bar(x_left: float, x_right: float, y_top: float, arrow_end: str) -> str:
    """折臂：等粗带子，末端一侧切一刀 → 读起来是「折」，不是方块。

    S 之所以一眼是 S，靠的是两道折臂的转折处有方向感：纯圆角太软、
    纯直角像积木。末端这一刀是 45° 还是 20°，决定它「折」得够不够——
    上一版试过 45°，看起来像一支铅笔。

    arrow_end="right" 时右端收尖（上臂），"left" 时左端收尖（下臂）。
    """
    c = AH * 0.55          # 倒角长度
    if arrow_end == "right":
        return (
            f"M{x_left:.1f} {y_top:.1f}"
            f"H{x_right - c:.1f}"
            f"L{x_right - c * 0.35:.1f} {y_top + AH / 2:.1f}"
            f"L{x_right - c:.1f} {y_top + AH:.1f}"
            f"H{x_left:.1f}Z"
        )
    return (
        f"M{x_right:.1f} {y_top:.1f}"
        f"H{x_left + c:.1f}"
        f"L{x_left + c * 0.35:.1f} {y_top + AH / 2:.1f}"
        f"L{x_left + c:.1f} {y_top + AH:.1f}"
        f"H{x_right:.1f}Z"
    )


def _bar(x_left: float, x_right: float, y_top: float, r_top_right: float = 0.0,
         r_bottom_left: float = 0.0) -> str:
    """一道折臂：横向等粗条。

    角是按角色定的，不是一刀切的圆角：末端（笔画的收尾处）圆润，
    另一端方着被腰盖住。四角一样圆会读成一根面条，
    四角一样方又太生硬——S 的「折」感就藏在这个差别里。
    """
    r = RC
    rtr = r_top_right or r
    rbl = r_bottom_left or r
    return (
        f"M{x_left + rbl:.1f} {y_top:.1f}"
        f"H{x_right - rtr:.1f}"
        f"A{rtr:g} {rtr:g} 0 0 1 {x_right:.1f} {y_top + rtr:.1f}"
        f"V{y_top + AH - r:.1f}"
        f"A{r:g} {r:g} 0 0 1 {x_right - r:.1f} {y_top + AH:.1f}"
        f"H{x_left + rbl:.1f}"
        f"A{rbl:g} {rbl:g} 0 0 1 {x_left:.1f} {y_top + AH - rbl:.1f}"
        f"V{y_top + r:.1f}"
        f"A{r:g} {r:g} 0 0 1 {x_left + r:.1f} {y_top:.1f}Z"
    )


AW, AH = 68.5, 17.0   # 折臂长度 / 粗细
XT0 = 18.0
XT1 = XT0 + AW                     # 上臂左右端
XB0 = XT0 + 4.0
XB1 = XB0 + AW                    # 下臂与上臂错开一点，S 才有重心
YT, YB = 22.0, 78.0                # 两道折臂的中心线

# 上臂：起点侧直角（和腰相接），末端圆角
BODY_TOP = _bevel_bar(XT0, XT1, YT - AH / 2, arrow_end="right")
# 下臂：镜像
BODY_BOT = _bevel_bar(XB0, XB1, YB - AH / 2, arrow_end="left")


def _s_alpha(x: float) -> float:
    """腰在 x 处的中心线高度。两端正好落在两道折臂上。"""
    k = (x - XT0) / (XB1 - XT0)
    return YT + (YB - YT) * k


def _slant_path() -> str:
    """中间的斜腰：从上臂右段连到下臂左段，**穿过中心**。

    这一笔是「S」成立的关键：它必须在中间斜穿过去，两道折臂才被连成
    S 形；停在旁边不穿过去，整枚图形就成了两条断开的折线（那正是上一版）。
    """
    x0, x1 = XT1 - 6.0, XB0 + 6.0
    r = SLANT / 2
    # 腰的两端用圆头：它是「穿过折臂」的一笔，端头藏在折臂里，
    # 圆头保证不管两条带的夹角多刁钻都不会露出尖角
    return f"M{x0:.1f} {_s_alpha(x0):.1f}L{x1:.1f} {_s_alpha(x1):.1f}"


PNG_SIZES = (16, 20, 24, 32, 48, 64, 96, 128, 180, 192, 256, 512, 1024)
MONO_SIZES = (16, 24, 32, 48, 64, 256)
MONO_INK = "#1b2233"
MONO_LIGHT = "#ffffff"
MASKABLE_SAFE = 0.2
SUPERSAMPLE = 8

# 图形包围盒：路径端点 + 箭头 + 描边外沿，再留出呼吸。
# 用固定值而不是解析路径——这条路径的极值点都在端点上，手填反而不会错。
# 笔画的真实包围盒。极值都落在端点/圆头上，但腰的圆头会探出半个线宽、
# 折臂的倒角点正好压在水平中线上——所以 x 侧要往外让 SLANT 的一半。
INK = (min(XT0, XB0) - SLANT / 2, YT - AH / 2,
       max(XT1, XB1) + SLANT / 2, YB + AH / 2)
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
    x0, y0, x1, y1 = X0, Y_TOP, X1 + 10, Y_BOT
    return (
        f'<linearGradient id="g" x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}"'
        f' gradientUnits="userSpaceOnUse">{stops}</linearGradient>'
    )


def body(fill: str) -> str:
    """一道 S 由三段合成：上臂、腰、下臂，末端再加一枚箭头。

    拆成三段是有原因的：腰的粗细和折臂的粗细不是一回事，一条 13 粗的斜线
    会把中间糊上半边画布；分开画才能让腰保持纤细、转折点干净。
    """
    return (
        f'<path d="{BODY_TOP}" fill="{fill}"/>'
        f'<path d="{BODY_BOT}" fill="{fill}"/>'
        f'<path d="{_slant_path()}" fill="none" stroke="{fill}"'
        f' stroke-width="{SLANT}" stroke-linecap="round"/>'
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
