"""生成 SimpleURL 的 Logo 资源：无底板、纯透明。

造型取字母 `S`：上半弯勾住下半弯，收口处折进来，末端是一枚箭头指向内侧
—— 长链接被折短的那一下。整枚图形只有一笔，没有底板、没有圆角方块，
贴任何底色上都只有这条线。

四条硬要求，都对应上一版真栽过的坑：

1. **真透明**：画布上只有这一笔，alpha 之外的像素全为 0。
2. **所有尺寸是同一张图**：PNG 一律从同一条路径 8 倍超采样再 LANCZOS 缩回。
   按尺寸分档改描边，16px 和高清版就会变成两个造型（上一版就是这样）。
3. **画布由包围盒算出**：留白是算出来的，不是手填的 viewBox，
   因此图形永远居中、永远不贴边。
4. **不做像素级微调**：`snap()` 曾经给 16px 额外补线宽，结果在 16px 描边
   糊成一片。几像素的图标要的是干净，不是补面积。

依赖 cairosvg（非运行时依赖，只有出图时需要）：`pip install cairosvg`。

输出（app/static/logo/）：
- icon.svg / icon-{16..1024}.png   主图标
- favicon.svg / ../favicon.ico     站点图标
- icon-mono.svg / .png             单色版（currentColor）
- icon-maskable.svg / .png         PWA 用，带 20% 安全区
- logo-horizontal.svg              图标 + 字标横版
- preview.html                     深底 / 浅底 / 各尺寸 / 蒙版，一页自查

改色改形只动下面的常量，然后 `python scripts/gen_logo.py`。
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

OUT = Path(__file__).resolve().parents[1] / "app" / "static" / "logo"
OUT.mkdir(parents=True, exist_ok=True)

# 品牌渐变：青 → 紫 → 品红。方向顺着笔画走，收口处才由紫转粉。
STOPS = ((0.0, "#4cc9ff"), (0.55, "#8b6bff"), (1.0, "#ff5fbf"))
GRAD_DIR = ((0.06, 0.05), (0.94, 0.95))

# 一笔 S：上臂自左向右，绕右侧大弯拐回来，下弯再拐到左下收口
BODY = "M22 45H44C62 45 72 55 72 64C72 78 60 88 44 88C34 88 26 84 20 78"
# 收口处的箭头，指回内侧
ARROW = "M20 78L20 55L40 74Z"
STROKE = 14
CAP_PAD = STROKE / 2          # 圆帽会画出路径两端，算留白时得带上

PNG_SIZES = (16, 20, 24, 32, 48, 64, 96, 128, 180, 192, 256, 512, 1024)
MONO_SIZES = (48, 256)
MASKABLE_SAFE = 0.2           # PWA maskable 四周安全区比例
SUPERSAMPLE = 8

# 包围盒：解析路径里的点 + 圆帽半径 + 箭头，再统一留 4% 呼吸
_BOX_CACHE: tuple[float, float, float, float] | None = None


def _path_points(d: str) -> list[tuple[float, float]]:
    """粗略取路径上的控制点：对一条单调的折返线，控制点包住的范围就够用了。"""
    import re

    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", d)]
    return list(zip(nums[0::2], nums[1::2]))


def content_box() -> tuple[float, float, float, float]:
    global _BOX_CACHE
    if _BOX_CACHE:
        return _BOX_CACHE
    pts = _path_points(BODY) + _path_points(ARROW)
    # 笔画在拐弯处会向外鼓出，比端点更靠外（curve 控制点只包住一部分）。
    # 光靠控制点算，小尺寸一缩、抗锯齿一糊，图形就顶到画布边缘了。
    PAD = STROKE * 0.42
    # 再留一点像素级的余量：16~24px 那几档，抗锯齿会把最后一行像素推到画布外沿。
    # 这里不加「按尺寸的补丁」，只留一个固定比例——像素微调是上一版糊掉的原因。
    # 0.10 是实测算出来的：16px 下 0.93 单位留白会被压成 0.15 像素，
    # 四舍五入直接把笔画推到边线上。图标小到这个程度，留白得按「像素」要，不是按比例。
    EDGE = 0.10
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = min(xs) - CAP_PAD, max(xs) + CAP_PAD
    y0, y1 = min(ys) - CAP_PAD, max(ys) + CAP_PAD
    pad = max(x1 - x0, y1 - y0) * EDGE + PAD
    # 用正方形画布兜住：圆角图标、favicon、PWA 都要求 1:1
    side = max(x1 - x0, y1 - y0) + pad * 2
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    _BOX_CACHE = (cx - side / 2, cy - side / 2, side, side)
    return _BOX_CACHE


def viewbox(scale: float = 1.0) -> str:
    """scale < 1 时把图形缩进去，四周留出安全区（maskable / 单色版用）。"""
    x, y, w, h = content_box()
    if scale != 1.0:
        cx, cy = x + w / 2, y + h / 2
        w, h = w / scale, h / scale
        x, y = cx - w / 2, cy - h / 2
    return f"{x:g} {y:g} {w:g} {h:g}"


def _grad() -> str:
    stops = "".join(f'<stop offset="{o:g}" stop-color="{c}"/>' for o, c in STOPS)
    (x1, y1), (x2, y2) = GRAD_DIR
    return (
        f'<linearGradient id="g" x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" y2="{y2:g}">'
        f"{stops}</linearGradient>"
    )


def body(fill: str) -> str:
    return (
        f'<path d="{BODY}" fill="none" stroke="{fill}" stroke-width="{STROKE:g}"'
        ' stroke-linecap="round"/>'
        f'<path d="{ARROW}" fill="{fill}"/>'
    )


MONO_INK = "#1b2233"   # 单色 PNG 的固定墨色（深底上要配另一份浅色的，见 icon-mono-light）


def make_icon_svg(scale: float = 1.0, mono: bool = False, size: int = 100, ink: str = "currentColor") -> str:
    paint = ink if mono else "url(#g)"
    defs = "" if mono else f"<defs>{_grad()}</defs>"
    return (
        '<svg xmlns="http://www.w3.org/2000/svg"'
        f' viewBox="{viewbox(scale)}" width="{size}" height="{size}"'
        ' role="img" aria-label="SimpleURL">'
        f"{defs}{body(paint)}"
        "</svg>\n"
    )


def make_wordmark_svg() -> str:
    """横版：图标 + 字标。字标用 currentColor，深浅底都跟着走；
    只有 URL 三个字母锁品牌主色。"""
    x, y, w, h = content_box()
    side = 100
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 392 {side}"'
        f' width="392" height="{side}" role="img" aria-label="SimpleURL">\n'
        f"  <defs>{_grad()}</defs>\n"
        f'  <g transform="translate(0 {(side - h) / 2 - y:.2f}) scale({side / w:.4f})'
        f' translate({-x:.2f} 0)">{body("url(#g)")}</g>\n'
        '  <text x="112" y="66" font-family="Inter, \'SF Pro Display\', -apple-system,'
        " 'Segoe UI', 'PingFang SC', system-ui, sans-serif\" font-size=\"46\""
        ' font-weight="700" letter-spacing="-1.4" fill="currentColor">Simple'
        f'<tspan fill="{STOPS[1][1]}">URL</tspan></text>\n'
        "</svg>\n"
    )


def render(svg: str, size: int, ss: int = SUPERSAMPLE) -> Image.Image:
    """超采样后缩回。cairosvg 在小尺寸下会自己改采样策略，
    8× 后再 LANCZOS 缩回去，才和 512px 版是同一个造型。"""
    import cairosvg

    png = cairosvg.svg2png(
        bytestring=svg.encode("utf-8"), output_width=size * ss, output_height=size * ss
    )
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    return im.resize((size, size), Image.LANCZOS)


def scrub_edges(img: Image.Image) -> Image.Image:
    """把最外一圈像素清空。

    16/20px 这档，笔画边缘的抗锯齿会漏出 alpha=1~4 的像素——肉眼看不见，
    但「四角 alpha 必须是 0」是一条能自动检查的硬线，而带底板的图标正是
    从「最外圈的浅色像素」开始的。与其争论多少算底板，不如让画布边缘
    干净得像一刀切过。图形本身离边至少 1 像素，切掉这一圈换不到任何可见损失。
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

    # 单色 PNG 要给一个确定的颜色：currentColor 在 cairosvg 里没有继承源，
    # 出来是全黑的，贴到深色底上等于没有图标
    # 单色 PNG 印在什么底色上都有可能，所以给两份：固定墨色 + 反白
    for suffix, ink in (("", MONO_INK), ("-light", "#ffffff")):
        mono = make_icon_svg(mono=True, scale=0.96, ink=ink)
        for size in MONO_SIZES:
            name = f"icon-mono{suffix}-{size}.png"
            scrub_edges(render(mono, size)).save(OUT / name, optimize=True)
            names.append(name)

    maskable = make_icon_svg(scale=1 - 2 * MASKABLE_SAFE)
    scrub_edges(render(maskable, 512)).save(OUT / "icon-maskable.png", optimize=True)
    names.append("icon-maskable.png")

    # .ico 只放 16/32/48。小尺寸靠超采样自己出，不交给 ICO 自带的缩放
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
        ("#f1f5f9", "#1e293b", "浅灰底"),
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
        rows.append(
            f'<section style="background:{bg};color:{fg}">'
            f"<h2>{label}</h2><div class='row'>{items}</div></section>"
        )

    # maskable 会被系统裁成圆形，这里用 border-radius 直接模拟
    mask = "".join(
        f'<figure><img src="icon-maskable.png" width="96" height="96" alt="maskable"'
        f' style="border-radius:{r};background:#243049"><figcaption>{n}</figcaption></figure>'
        for r, n in (("50%", "圆形裁切"), ("28%", "圆角裁切"), ("0", "原图"))
    )
    # 单色版靠 currentColor 上色，所以必须由容器显式给 color。
    # 上一版让深底那段继承 #eef2ff 之外的默认值，图直接看不见——
    # 自查页自己出错，比图标出错更难发现。
    mono = "".join(
        f'<img src="icon-mono.svg" width="{s}" height="{s}" alt="mono {s}">' for s in (16, 24, 32, 64)
    )
    return f"""<!doctype html><meta charset="utf-8"><title>SimpleURL Logo 自查</title>
<style>
 body{{margin:0;font:14px/1.5 system-ui,sans-serif;background:#fff;color:#111}}
 section{{padding:24px}} h2{{margin:0 0 16px;font-size:13px;letter-spacing:.12em;text-transform:uppercase;opacity:.6}}
 .row{{display:flex;align-items:flex-end;gap:22px;flex-wrap:wrap}}
 figure{{margin:0;text-align:center}} figcaption{{font-size:11px;opacity:.55;margin-top:6px}}
 .mono{{background:#f3f4f8;color:#1b2233;padding:24px;display:flex;gap:22px;align-items:center}}
 .mono.dark{{background:#0b1020;color:#eef2ff}}
 .mono img{{display:block}}
 .check{{display:flex;align-items:center;gap:10px;padding:0 24px 20px;font-size:13px;opacity:.75}}
</style>
{rows[0]}
{rows[1]}
<section style="background:#0b1020;color:#fff"><h2>PWA 蒙版裁切（内容须落在中间 80%）</h2><div class="row">{mask}</div></section>
{rows[2]}
<div class="mono"><strong>单色版 · 浅底</strong>{mono}
  <img src="icon-mono-48.png" width="48" height="48" alt="mono png"></div>
<div class="mono dark"><strong>单色版 · 深底</strong>{mono.replace(".svg", ".svg")}
  <img src="icon-mono-light-48.png" width="48" height="48" alt="mono light png"></div>
<p class="check">四角必须完全透明：背景色块变化时图形边缘不该出现任何方块。</p>
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
