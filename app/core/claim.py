"""领取码：开放模式下，访客也能回看自己刚建的那一条链接。

开放模式不公开链接库，但「我建的链接看得怎么样」这种需求必须满足。
做法是创建时下发一个随机领取码（明文只出现一次），哈希落库；
之后带 `?t=<领取码>` 就能看这一条的详情与统计。过期、停用、删除之后
领取码一并失效——它不是第二套账号体系，只是一张一次性的回执。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Link

logger = logging.getLogger("simpleurl")

TOKEN_LENGTH = 32


def issue(db: Session, link: Link) -> str:
    """签发领取码，返回明文（只此一次）。"""
    token = secrets.token_urlsafe(24)[:TOKEN_LENGTH]
    link.claim_hash = _hash(token)
    db.flush()
    return token


def verify(db: Session, link: Link, token: str) -> bool:
    """校验领取码是否属于该链接。恒定时间比较，不泄露前缀。"""
    if not token or not link or not link.claim_hash:
        return False
    return hmac.compare_digest(link.claim_hash, _hash(token))


def revoke(db: Session, link: Link) -> None:
    link.claim_hash = None
    db.flush()


def _hash(token: str) -> str:
    # 领取码本身是 144 位随机，够抗暴力，不需要慢哈希拖慢每次查看
    salt = settings.session_secret or "simpleurl"
    return hashlib.sha256(f"{salt}:{token}".encode()).hexdigest()


def param_name() -> str:
    return "t"
