"""运行参数存储：默认值来自环境变量，后台改动落到 settings 表。"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Setting

logger = logging.getLogger("simpleurl")

# 可在后台调整的参数：键 -> (默认值, 说明)
EDITABLE: dict[str, tuple[object, str]] = {
    "site_name": ("SimpleURL", "站点名称"),
    "redirect_rate_limit": (120, "单 IP 每分钟跳转上限，0 不限制"),
    "click_daily_limit": (0, "单 IP 每日点击上限，0 不限制"),
    "fetch_title": (True, "创建时自动获取目标页标题"),
    "require_api_key": (False, "要求 API 请求携带密钥"),
    "create_daily_limit": (200, "单 IP 每日创建上限，0 不限制"),
    "access_mode": ("open", "访问模式：open 开放外部可使用，admin 仅管理员可使用"),
    "announcement": ("", "站点公告，显示在首页顶部；留空不显示"),
    "code_length": (4, "自动生成短码的长度（3~16）"),
    "site_tagline": ("数据可自托管，隐私尽在掌握", "页脚自述"),
    # ---------- 社交平台防洪 ----------
    "social_guard": (True, "社交平台防洪总开关（微信 / QQ 等）"),
    "social_block_hint": (
        "该链接暂时无法在此处打开，请点击右上角选择「在浏览器中打开」，或复制链接后重试。",
        "社交平台拦截页的提示语",
    ),
    # 标记档：照常跳转、照常计数，只是把来源平台记下来（默认开）
    "social_tag_qq": (True, "标记 QQ 来源（照常放行计数）"),
    "social_tag_wechat": (True, "标记微信来源（照常放行计数）"),
}


def _to_str(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def code_length_value(db: Session) -> int:
    """生效的自动短码长度，顺手收紧到合法区间。"""
    try:
        raw = int(effective(db, "code_length"))
    except (TypeError, ValueError):
        raw = 4
    return max(3, min(16, raw))


def _cast(key: str, raw: str, default: object) -> object:
    """按默认值的类型还原：bool 认 0/1，int 走 int，字符串原样返回。"""
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        try:
            return int(raw.strip())
        except (TypeError, ValueError):
            return default
    if key == "access_mode":
        from app.core.access import normalize_mode

        return normalize_mode(raw)
    if key == "announcement":
        # 空串是合法值（表示不显示公告），不能像别的字符串那样回落默认
        return raw.strip()
    return raw.strip() or default


def get_all(db: Session) -> dict[str, object]:
    """生效配置：环境变量只是出厂默认，数据库里的覆盖优先。"""
    values: dict[str, object] = {k: d[0] for k, d in EDITABLE.items()}
    for key, value in _load_overrides(db).items():
        values[key] = value
    return values


def _default_source():
    """环境变量默认值的来源，延迟导入避免循环依赖。"""
    from app.core.config import settings as env_settings

    return env_settings


def _load_overrides(db: Session) -> dict[str, object]:
    if db is None:
        return {}
    rows = db.execute(select(Setting)).scalars().all()
    out: dict[str, object] = {}
    for row in rows:
        if row.key not in EDITABLE:
            continue
        default = EDITABLE[row.key][0]
        out[row.key] = _cast(row.key, row.value, default)
    return out


def effective(db: Session, key: str) -> object:
    """取单个参数的生效值：数据库覆盖 > 环境变量 > 代码默认。"""
    default = EDITABLE.get(key, (None, ""))[0]
    if db is None:
        return getattr(_default_source(), key, default)
    row = db.execute(select(Setting.value).where(Setting.key == key)).scalar_one_or_none()
    if row is not None:
        return _cast(key, row, default)
    return getattr(_default_source(), key, default)


def set_value(db: Session, key: str, value: object) -> None:
    if key not in EDITABLE:
        raise KeyError(key)
    row = db.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
    if row is None:
        row = Setting(key=key, value=_to_str(value))
        db.add(row)
    else:
        row.value = _to_str(value)
    db.flush()
    logger.info("配置项 %s 已更新为 %s", key, value)


def reset_value(db: Session, key: str) -> None:
    row = db.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
    if row is not None:
        db.delete(row)
        db.flush()
