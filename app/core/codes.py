"""短码生成：安全随机 + 去重重试。"""

from __future__ import annotations

import secrets
import string

from app.core.db import session_scope
from app.models import Link

ALPHABET = string.ascii_letters + string.digits  # 62 进制，URL 安全
AMBIGUOUS = "0O1lI"  # 避免视觉混淆的字符


def _pick_alphabet(no_ambiguous: bool = True) -> str:
    if not no_ambiguous:
        return ALPHABET
    return "".join(ch for ch in ALPHABET if ch not in AMBIGUOUS)


def random_code(length: int = 6, no_ambiguous: bool = True) -> str:
    alphabet = _pick_alphabet(no_ambiguous)
    return "".join(secrets.choice(alphabet) for _ in range(length))


def normalize_code(raw: str) -> str:
    """统一短码格式：去首尾空白。"""
    if raw is None:
        return ""
    return str(raw).strip()


def validate_custom_code(code: str, min_len: int, max_len: int) -> tuple[bool, str]:
    """校验自定义短码，返回 (是否合法, 错误信息)。"""
    from app.core.config import settings

    if not code:
        return False, "短码不能为空"
    if len(code) < min_len:
        return False, f"短码至少 {min_len} 位"
    if len(code) > max_len:
        return False, f"短码最多 {max_len} 位"
    if not all(ch.isalnum() or ch in "-_" for ch in code):
        return False, "短码仅支持字母、数字、- 和 _"
    lowered = code.lower()
    if lowered in {"api", "admin", "static", "healthz", "favicon.ico", "robots.txt"}:
        return False, "该短码为系统保留字"
    return True, ""


def code_exists(code: str) -> bool:
    """短码是否已被占用。

    必须**不分大小写**：表上那个 unique 索引是区分大小写的，`CNB` 和 `cnb`
    在 SQLite / Postgres 眼里是两条不同的记录，可短链域名不区分大小写——
    这两个短码最终指向同一个地址，谁写进去谁赢。生成时要跟着这个语义走。
    """
    from sqlalchemy import func, select

    with session_scope() as db:
        return db.execute(select(Link.id).where(func.lower(Link.code) == code.lower())).first() is not None


def generate_unique_code(length: int | None = None, no_ambiguous: bool = True, max_tries: int = 50) -> str:
    """生成未占用的短码。"""
    from app.core.config import settings

    length = length or settings.code_length
    for _ in range(max_tries):
        candidate = random_code(length, no_ambiguous)
        if not code_exists(candidate):
            return candidate
    # 兜底：加长再试
    for _ in range(max_tries):
        candidate = random_code(length + 4, no_ambiguous)
        if not code_exists(candidate):
            return candidate
    raise RuntimeError("无法生成唯一短码，请稍后重试")
