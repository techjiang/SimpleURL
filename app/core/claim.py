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


def ensure(db: Session, link: Link) -> tuple[str, bool]:
    """确保这条链接有回执；已经有就**原样保留**那一张。

    返回 (明文, 是否新签发)。已有的那张拿不到明文（库里只有哈希），
    所以复用路径必须走「用旧换新」——`reissue()`，不能走这里。

    为什么需要这个：创建接口带 `reuse: true` 或短码撞车时会把现成的
    那条还回去，如果这里也重新签一张，第一个人手里的回执就凭空作废了：
    他只是想复用自己刚建的那条，回去一刷新，面板没了、页面提示
    「回执已被收回」，而他从头到尾没做错任何事。
    """
    if link.claim_hash:
        return "", False
    return issue(db, link), True


def verify(db: Session, link: Link, token: str) -> bool:
    """校验领取码是否属于该链接。恒定时间比较，不泄露前缀。"""
    if not token or not link or not link.claim_hash:
        return False
    return hmac.compare_digest(link.claim_hash, _hash(token))


def reissue(db: Session, link: Link) -> str:
    """用旧回执换一张新的：旧的立刻作废，新的只出现这一次。"""
    return issue(db, link)


def revoke(db: Session, link: Link) -> None:
    link.claim_hash = None
    db.flush()


def _hash(token: str) -> str:
    # 领取码本身是 144 位随机，够抗暴力，不需要慢哈希拖慢每次查看
    salt = settings.session_secret or "simpleurl"
    return hashlib.sha256(f"{salt}:{token}".encode()).hexdigest()


def param_name() -> str:
    return "t"


# ---------- 线索登记：丢了回执怎么找回来 ----------
#
# 回执丢了就是丢了（服务端只存哈希）。留一个「线索」作为找回的凭据，
# 但刻意做成「弱恢复」而不是「账号」：
#   · 线索本身也存哈希，不落明文
#   · 反查必须重新提供同一个线索
#   · 每次反查只为**最近一条**链接重签，且 1 小时内最多 3 次
# 这样最坏情况是「知道邮箱的人碰运气看到你最新那条」，而不是整份清单被端走。

CLAIM_LOOKUP_WINDOW = 3600
CLAIM_LOOKUP_MAX = 3
CLAIM_LOOKUP_DAYS = 30


def normalize_clue(raw: str) -> str:
    return " ".join(str(raw or "").strip().lower().split())


def clue_hash(raw: str) -> str:
    salt = settings.session_secret or "simpleurl"
    return hashlib.sha256(f"clue:{salt}:{normalize_clue(raw)}".encode()).hexdigest()


def register_clue(db: Session, link, raw: str) -> None:
    """登记一条找回线索。同一线索重复登记只留最新的那几行，不无限长。"""
    from sqlalchemy import delete, select

    from app.models import ClaimRecord

    clue = normalize_clue(raw)
    if not clue:
        return
    db.add(ClaimRecord(link_id=link.id, clue_hash=clue_hash(clue)))
    db.flush()
    # 每个线索只留最近 5 条：留太多等于把一个人的链接清单攒起来
    rows = (
        db.execute(
            select(ClaimRecord.id)
            .where(ClaimRecord.clue_hash == clue_hash(clue))
            .order_by(ClaimRecord.id.desc())
            .offset(5)
        )
        .scalars()
        .all()
    )
    if rows:
        db.execute(delete(ClaimRecord).where(ClaimRecord.id.in_(list(rows))))
        db.flush()


def lookup(db: Session, raw: str) -> object | None:
    """按线索找回最近一条还有回执的链接。

    先查线索命中，再排除已删 / 已过期 / 租期外的，最后取最新的一条。
    """
    import datetime as dt

    from sqlalchemy import select

    from app.core.timeutil import utcnow
    from app.models import ClaimRecord, Link

    clue = normalize_clue(raw)
    if not clue:
        return None
    since = utcnow() - dt.timedelta(days=CLAIM_LOOKUP_DAYS)
    stmt = (
        select(Link)
        .join(ClaimRecord, ClaimRecord.link_id == Link.id)
        .where(
            ClaimRecord.clue_hash == clue_hash(clue),
            ClaimRecord.created_at >= since,
            Link.claim_hash.is_not(None),
            Link.enabled.is_(True),
        )
        .order_by(ClaimRecord.id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


def lookup_allowed(ip: str | None) -> bool:
    """反查限速：同一 IP 1 小时内 3 次。

    不加限速的话，这个接口就是一台「拿邮箱试短码」的机器，
    线索再弱也白搭。
    """
    from app.core.security import rate_limiter

    return rate_limiter.allow(f"claim-lookup:{ip or 'unknown'}", CLAIM_LOOKUP_MAX, CLAIM_LOOKUP_WINDOW)
