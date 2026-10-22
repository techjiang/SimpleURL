"""二维码生成。"""

from __future__ import annotations

import io

import qrcode
from qrcode.constants import ERROR_CORRECT_M


def make_qr_png(data: str, box_size: int = 8, border: int = 2) -> bytes:
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=box_size, border=border)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0f172a", back_color="white").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
