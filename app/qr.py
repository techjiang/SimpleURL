"""二维码生成。

默认只出 240px 的 PNG；贴海报、发印刷品时那个尺寸不够看。这里补上
尺寸与格式两档，并顺手做了「静区留白」和中间的小 Logo——二维码中间放品牌
图标是行规，错误纠正等级开到 H（30% 冗余）才不会因为挖掉一块而扫不出来。
"""

from __future__ import annotations

import io

import qrcode
from qrcode.constants import ERROR_CORRECT_H, ERROR_CORRECT_M

# 允许的边长（像素）：够小够大，又不至于让人一次要出一张 4000px 的图
QR_SIZES = (160, 240, 320, 480, 640, 800, 1024)
QR_FORMATS = ("png", "svg")


def nearest_size(value: int) -> int:
    """取最接近的一档边长：不做无限阶梯，缓存才命中得了。"""
    return min(QR_SIZES, key=lambda s: abs(s - int(value or QR_SIZES[0])))


def _build(data: str, level: int, border: int) -> qrcode.QRCode:
    qr = qrcode.QRCode(version=None, error_correction=level, box_size=1, border=border)
    qr.add_data(data)
    qr.make(fit=True)
    return qr


def make_qr_png(data: str, box_size: int = 8, border: int = 2, pixel_size: int | None = None, logo: bool = False) -> bytes:
    """出 PNG。给了 pixel_size 就按目标边长反推每格像素，图片不会糊。"""
    level = ERROR_CORRECT_H if logo else ERROR_CORRECT_M
    qr = _build(data, level, border)
    modules = qr.modules_count + border * 2
    if pixel_size:
        box_size = max(1, round(pixel_size / modules))
    qr.box_size = box_size
    img = qr.make_image(fill_color="#0f172a", back_color="white").convert("RGB")
    if logo:
        img = _paste_logo(img)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_qr_svg(data: str, border: int = 2, logo: bool = False) -> bytes:
    """出 SVG：矢量，印刷与深色底都能用，边框留白一样保留。"""
    level = ERROR_CORRECT_H if logo else ERROR_CORRECT_M
    qr = _build(data, level, border)
    matrix = qr.get_matrix()
    size = len(matrix)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'width="512" height="512" shape-rendering="crispEdges" role="img" aria-label="QR code">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
    ]
    for y, row in enumerate(matrix):
        run_start = None
        for x, dark in enumerate(row + [False]):
            if dark and run_start is None:
                run_start = x
            elif not dark and run_start is not None:
                # 同一行相邻的黑格合成一条矩形，文件小一半
                parts.append(
                    f'<rect x="{run_start}" y="{y}" width="{x - run_start}" height="1" fill="#0f172a"/>'
                )
                run_start = None
    parts.append("</svg>")
    return "".join(parts).encode()


def _paste_logo(img):
    """把品牌图标贴到正中。

    占比控制在 1/5 以内：错误纠正开到 H 能容下约 30% 的遮挡，
    再大就属于「扫不出来但看着挺好看」。
    """
    from pathlib import Path

    from PIL import Image

    icon_path = Path(__file__).resolve().parent / "static" / "logo" / "icon-256.png"
    if not icon_path.exists():
        return img
    side = int(img.width * 0.2)
    if side < 12:
        return img
    icon = Image.open(icon_path).convert("RGBA").resize((side, side), Image.LANCZOS)
    pad = max(2, side // 12)
    plate = Image.new("RGBA", (side + pad * 2, side + pad * 2), (255, 255, 255, 255))
    base = img.convert("RGBA")
    plate.paste(icon, (pad, pad), icon)
    box = plate.resize((side + pad * 2, side + pad * 2), Image.LANCZOS)
    pos = ((base.width - box.width) // 2, (base.height - box.height) // 2)
    base.alpha_composite(box, pos)
    return base.convert("RGB")
