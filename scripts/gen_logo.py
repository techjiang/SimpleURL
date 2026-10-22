"""生成 SimpleURL 无背景 Logo（SVG + 多尺寸 PNG）。

设计理念：
- 主体是一个"圆角方形令牌"，象征被收纳起来的链接；
- 内部是"折返箭头 + 端点"的造型，表达"短链跳转、一键抵达"；
- 渐变取自产品主题色（蓝 → 紫 → 品红），深/浅底色通用。
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "app" / "static" / "logo"
OUT.mkdir(parents=True, exist_ok=True)

BLUE = "#4f8cff"
PURPLE = "#7b5cff"
PINK = "#d946ef"

GRAD_DEF = """<linearGradient id="gMain" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{b}"/><stop offset=".55" stop-color="{p}"/><stop offset="1" stop-color="{k}"/>
  </linearGradient>"""


def badge_body(fill: str = "url(#gMain)", highlight: bool = True, stroke_width: int = 9) -> str:
    hl = (
        '\n  <rect x="3.75" y="3.75" width="88.5" height="88.5" rx="23.25" fill="none" '
        'stroke="#ffffff" stroke-opacity=".28" stroke-width="1.5"/>'
        if highlight
        else ""
    )
    return f"""  <rect x="3" y="3" width="90" height="90" rx="24" fill="{fill}"/>{hl}
  <path d="M34 44.5h20.5a8.5 8.5 0 0 1 0 17H52" fill="none" stroke="#fff" stroke-width="{stroke_width}"
        stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M45.5 66.5 37 58.5l8.5-8" fill="none" stroke="#fff" stroke-width="{stroke_width}"
        stroke-linecap="round" stroke-linejoin="round"/>"""


def make_icon_svg() -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="96" height="96" role="img" aria-label="SimpleURL">
  <defs>{GRAD_DEF.format(b=BLUE, p=PURPLE, k=PINK)}</defs>
{badge_body()}
</svg>
"""


def make_favicon_svg() -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="96" height="96" role="img" aria-label="SimpleURL">
  <defs>{GRAD_DEF.format(b=BLUE, p=PURPLE, k=PINK)}</defs>
{badge_body(highlight=False, stroke_width=10)}
</svg>
"""


def make_mono_svg() -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="96" height="96" role="img" aria-label="SimpleURL">
{badge_body(fill="currentColor", highlight=False)}
</svg>
"""


def make_wordmark_svg() -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 340 96" width="340" height="96" role="img" aria-label="SimpleURL">
  <defs>{GRAD_DEF.format(b=BLUE, p=PURPLE, k=PINK)}
    <linearGradient id="gText" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#c9d6ee"/>
    </linearGradient>
  </defs>
{badge_body()}
  <text x="112" y="61" font-family="Inter, 'SF Pro Display', -apple-system, 'Segoe UI', 'PingFang SC', system-ui, sans-serif"
        font-size="42" font-weight="700" letter-spacing="-1.3" fill="url(#gText)">Simple<tspan fill="{BLUE}">URL</tspan></text>
</svg>
"""


def render_pngs() -> list[str]:
    import cairosvg

    svg = make_icon_svg().encode()
    names: list[str] = []
    for size in (16, 32, 48, 64, 96, 128, 180, 192, 256, 512):
        name = f"icon-{size}.png"
        cairosvg.svg2png(bytestring=svg, write_to=str(OUT / name), output_width=size, output_height=size)
        names.append(name)
    # favicon.ico 用 32/48/64 多尺寸合成
    try:
        from PIL import Image

        sizes = [(16, Image.open(OUT / "icon-16.png")), (32, Image.open(OUT / "icon-32.png")),
                 (48, Image.open(OUT / "icon-48.png"))]
        ico_path = OUT.parent / "favicon.ico"
        sizes[0][1].save(
            ico_path, format="ICO", sizes=[(s, s) for s, _ in sizes],
            append_images=[img.resize((s, s), Image.LANCZOS) for s, img in sizes[1:]],
        )
        names.append("../favicon.ico")
    except Exception as exc:  # noqa: BLE001
        print("ICO 生成跳过:", exc)
    return names


def main() -> None:
    (OUT / "icon.svg").write_text(make_icon_svg(), encoding="utf-8")
    (OUT / "favicon.svg").write_text(make_favicon_svg(), encoding="utf-8")
    (OUT / "icon-mono.svg").write_text(make_mono_svg(), encoding="utf-8")
    (OUT / "logo-horizontal.svg").write_text(make_wordmark_svg(), encoding="utf-8")
    print("生成:", ", ".join(render_pngs()))


if __name__ == "__main__":
    main()
