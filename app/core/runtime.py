"""运行期配置：后台可改、落库持久化、DB 优先于环境变量。

设计上刻意只把「运营参数」放进来——访问模式、防洪阈值、站点名这类可以随时调的；
密钥、数据库连接这类改了要重启的仍留在环境变量里。所有值都带类型与上下界，
后台传什么脏数据都只会被夹回合法区间，不会让站点行为失控。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from sqlalchemy import select

from app.core.config import settings
from app.core.db import session_scope
from app.models import SettingsRow

logger = logging.getLogger("simpleurl")

TABLE = "app_settings"


class Option:
    """一个可编辑配置项。"""

    __slots__ = ("key", "label", "kind", "default", "lo", "hi", "help", "env")

    def __init__(
        self,
        key: str,
        label: str,
        kind: str,
        default: Any,
        *,
        lo: float | None = None,
        hi: float | None = None,
        help: str = "",
        env: Callable[[], Any] | None = None,
    ):
        self.key = key
        self.label = label
        self.kind = kind  # str | int | bool | select | text
        self.default = default
        self.lo = lo
        self.hi = hi
        self.help = help
        self.env = env  # 未落库时的取值来源（通常是环境变量）


def _s(key: str, label: str, default: str, **kw) -> Option:
    return Option(key, label, "str", default, **kw)


OPTIONS: tuple[Option, ...] = (
    _s("site_name", "站点名称", "SimpleURL", hi=40, help="顶栏与页面标题显示的名字"),
    Option(
        "access_mode", "访问模式", "select", "public", lo=None,
        help="public 开放外部可使用 / admin 仅管理员可用 / invite 邀请码可用",
        env=lambda: settings.access_mode,
    ),
    Option("code_length", "自动短码长度", "int", 6, lo=3, hi=32),
    Option("create_daily_limit", "单 IP 每日创建上限", "int", 200, lo=0, hi=100000,
           help="0 表示不限制", env=lambda: settings.create_daily_limit),
    Option("guard_ip_per_minute", "单 IP 每分钟跳转上限", "int", 120, lo=0, hi=100000,
           help="超出返回 429 友好页；0 关闭", env=lambda: settings.guard_ip_per_minute),
    Option("guard_ip_daily", "单 IP 每日点击配额", "int", 20000, lo=0, hi=10_000_000,
           help="0 关闭", env=lambda: settings.guard_ip_daily),
    Option("guard_code_per_minute", "单短码每分钟点击上限", "int", 0, lo=0, hi=100000,
           help="热链接被打爆时先牺牲它，保站点存活；0 关闭", env=lambda: settings.guard_code_per_minute),
    Option("guard_block_seconds", "超限封禁时长（秒）", "int", 60, lo=1, hi=86400,
           env=lambda: settings.guard_block_seconds),
    Option("click_flush_interval", "点击落库间隔（秒）", "int", 5, lo=1, hi=60,
           env=lambda: settings.click_flush_interval),
    Option("click_flush_batch", "点击落库批量阈值", "int", 200, lo=1, hi=5000,
           env=lambda: settings.click_flush_batch),
    Option("blocked_ua_enabled", "启用 UA 屏蔽", "bool", True, env=lambda: settings.blocked_ua_enabled),
    _s("blocked_uas", "UA 黑名单（每行一条，支持 * 通配）", "", help="命中仍跳转，但不计数，后台留痕"),
    Option("wechat_mode", "微信内打开", "select", "warn",
           help="warn 放行并提示复制到浏览器 / blocked 一律拦截 / off 不处理"),
    Option("qq_mode", "QQ 内打开", "select", "warn",
           help="warn 放行并提示复制到浏览器 / blocked 一律拦截 / off 不处理"),
    Option("api_key_required", "强制 API 密钥", "bool", False,
           help="开启后所有 /api/* 请求必须带 X-Api-Key"),
    Option("auto_title", "自动获取标题", "bool", True, help="创建时抓目标页 <title>，抓不到用 URL 兜底"),
    _s("announcement", "站点公告", "", help="留空则不显示；显示在首页顶部"),
)

BY_KEY = {o.key: o for o in OPTIONS}
EDITABLE_KEYS = tuple(BY_KEY)


def _coerce(option: Option, raw: Any) -> Any:
    """把任意输入夹成合法值。宁可用默认值，也不让脏配置把站点搞挂。"""
    if option.kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if option.kind == "int":
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            value = int(option.default)
        if option.lo is not None:
            value = max(int(option.lo), value)
        if option.hi is not None:
            value = min(int(option.hi), value)
        return value
    text = "" if raw is None else str(raw)
    if option.kind == "select":
        allowed = {
            "access_mode": ("public", "admin", "invite"),
            "wechat_mode": ("off", "warn", "blocked"),
            "qq_mode": ("off", "warn", "blocked"),
        }.get(option.key, ())
        text = text.strip().lower()
        return text if text in allowed else option.default
    text = text.strip()
    if option.hi:
        text = text[: int(option.hi)]
    return text


_cache: dict[str, Any] = {}


def _default_of(option: Option) -> Any:
    if option.env is not None:
        return option.env()
    return option.default


def load() -> None:
    """从库里载入覆盖值。表不存在或读取失败都退回默认值，绝不因此启动失败。"""
    _cache.clear()
    try:
        with session_scope() as db:
            rows = db.execute(select(SettingsRow.key, SettingsRow.value)).all()
    except Exception as exc:  # pragma: no cover - 只在库异常时走到
        logger.warning("读取运行配置失败，改用默认值：%s", exc)
        return
    for key, value in rows:
        option = BY_KEY.get(key)
        if option is None:
            continue
        try:
            _cache[key] = _coerce(option, json.loads(value))
        except (ValueError, TypeError):
            continue
    logger.info("运行配置已载入，覆盖 %d 项", len(_cache))


def get(key: str) -> Any:
    option = BY_KEY.get(key)
    if option is None:
        raise KeyError(key)
    if key in _cache:
        return _cache[key]
    return _default_of(option)


def get_int(key: str) -> int:
    return int(get(key))


def get_bool(key: str) -> bool:
    return bool(get(key))


def get_str(key: str) -> str:
    return str(get(key) or "")


def set_many(values: dict[str, Any]) -> dict[str, Any]:
    """批量写入并落库，返回实际生效的值。"""
    clean: dict[str, Any] = {}
    for key, value in values.items():
        option = BY_KEY.get(key)
        if option is None:
            continue
        clean[key] = _coerce(option, value)
    if not clean:
        return {}
    with session_scope() as db:
        for key, value in clean.items():
            row = db.get(SettingsRow, key)
            payload = json.dumps(value, ensure_ascii=False)
            if row is None:
                db.add(SettingsRow(key=key, value=payload))
            else:
                row.value = payload
    _cache.update(clean)
    logger.info("运行配置已更新：%s", ", ".join(clean))
    return clean


def snapshot() -> dict[str, Any]:
    """给后台渲染用的当前值。"""
    return {key: get(key) for key in EDITABLE_KEYS}


def describe() -> list[dict]:
    out = []
    for option in OPTIONS:
        out.append(
            {
                "key": option.key,
                "label": option.label,
                "kind": option.kind,
                "value": get(option.key),
                "help": option.help,
                "lo": option.lo,
                "hi": option.hi,
            }
        )
    return out


def reset() -> None:
    _cache.clear()


def ua_list() -> list[str]:
    raw = get_str("blocked_uas")
    return [line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()]


def blocked_uas() -> list[str]:
    """生效的 UA 黑名单：环境变量在前，后台配置在后。"""
    merged = list(settings.blocked_uas)
    for item in ua_list():
        if item not in merged:
            merged.append(item)
    return merged


