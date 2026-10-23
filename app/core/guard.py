"""防洪与 UA 屏蔽。"""

from __future__ import annotations

import fnmatch
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import rate_limiter
from app.models import Setting

logger = logging.getLogger("simpleurl")

_UA_RULES_KEY = "blocked_ua"


def _rows(db: Session, key: str) -> list[str]:
    if db is None:
        return []
    raw = db.execute(select(Setting.value).where(Setting.key == key)).scalar_one_or_none()
    return parse_list(raw)


def parse_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()]


def serialize_list(items: list[str]) -> str:
    return "\n".join(dict.fromkeys(i.strip() for i in items if i.strip()))


def _ua_rules(db: Session) -> list[str]:
    return _rows(db, _UA_RULES_KEY)


def save_ua_rules(db: Session, rules: list[str]) -> None:
    row = db.execute(select(Setting).where(Setting.key == _UA_RULES_KEY)).scalar_one_or_none()
    value = serialize_list(rules)
    if row is None:
        db.add(Setting(key=_UA_RULES_KEY, value=value))
    else:
        row.value = value
    db.flush()
    logger.info("UA 屏蔽规则已更新：%d 条", len(rules))


def is_ua_blocked(ua: str | None, rules: list[str]) -> str | None:
    """命中则返回规则原文，没命中返回 None。支持 * 通配。"""
    if not ua or not rules:
        return None
    lowered = ua.lower()
    for rule in rules:
        r = rule.lower()
        if any(ch in r for ch in "*?["):
            if fnmatch.fnmatchcase(lowered, r):
                return rule
        elif r in lowered:
            return rule
    return None


def blocked_ua_rules(db: Session) -> list[str]:
    return _ua_rules(db)


def check_redirect_quota(db: Session, ip: str | None) -> tuple[bool, str | None]:
    """跳转限速：单 IP 每分钟上限。返回 (是否放行, 拒绝原因)。"""
    from app.core.settings_store import effective

    per_minute = effective(db, "redirect_rate_limit")
    if not ip or not per_minute or per_minute <= 0:
        return True, None
    if not rate_limiter.allow(f"redirect:{ip}", per_minute, 60):
        return False, f"访问过于频繁（每分钟上限 {per_minute} 次），请稍后再试"
    return True, None


def check_click_quota(db: Session, ip: str | None) -> tuple[bool, str | None]:
    """点击数配额：单 IP 每日上限，超出后不计点击。"""
    from app.core.settings_store import effective

    daily = effective(db, "click_daily_limit")
    if not ip or not daily or daily <= 0:
        return True, None
    if not rate_limiter.allow(f"click:{ip}", daily, 86400):
        return False, f"今日点击次数已达上限（{daily} 次）"
    return True, None


def reset_guard() -> None:
    rate_limiter.reset()
