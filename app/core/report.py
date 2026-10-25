"""操作审计日志：谁在什么时候对哪条链接做了什么。

短链接服务最常见的「事故」不是宕机，是「我的链接怎么跳到别处去了」。
没有留痕就只能靠猜。这里把写操作统一记一条，后台按时间倒序列出来。

只记动作与对象，不记 IP 明文——和点击统计一样，IP 只存哈希。
"""

from __future__ import annotations

import json
from typing import Iterable

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import client_ip, ip_hash

ACTIONS = {
    "login": "登录后台",
    "logout": "退出登录",
    "create": "创建链接",
    "update": "修改链接",
    "delete": "删除链接",
    "import": "导入 YOURLS",
    "settings": "修改设置",
    "flush": "结算点击",
    "claim": "回执换新",
}

MAX_ROWS = 2000


def record(db: Session, action: str, *, actor: str | None = None, code: str | None = None,
           detail: str | None = None, request: Request | None = None) -> None:
    """记一条操作留痕。任何异常都不该影响主流程，所以整段兜住。"""
    from app.models import AuditLog

    try:
        ip = client_ip(request) if request is not None else None
        db.add(
            AuditLog(
                action=action[:32],
                actor=(actor or "匿名")[:64],
                code=(code or None) and code[:32],
                detail=(detail or "")[:500] or None,
                ip_hash=ip_hash(ip) or None,
            )
        )
        db.flush()
    except Exception:  # noqa: BLE001 - 留痕失败不该让业务失败
        pass


def recent(db: Session, limit: int = 40, action: str | None = None) -> list[dict]:
    from app.models import AuditLog

    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(max(1, min(200, limit)))
    if action:
        stmt = stmt.where(AuditLog.action == action)
    out = []
    for row in db.execute(stmt).scalars().all():
        out.append(
            {
                "id": row.id,
                "action": row.action,
                "action_label": ACTIONS.get(row.action, row.action),
                "actor": row.actor,
                "code": row.code,
                "detail": row.detail,
                "ts": row.ts.isoformat() if row.ts else None,
            }
        )
    return out


def prune(db: Session) -> None:
    """只留最近 MAX_ROWS 条，长跑进程的日志表不能只涨不落。"""
    from app.models import AuditLog

    ids = list(
        db.execute(select(AuditLog.id).order_by(AuditLog.id.desc()).offset(MAX_ROWS)).scalars().all()
    )
    for chunk_start in range(0, len(ids), 500):
        chunk = ids[chunk_start : chunk_start + 500]
        for row_id in chunk:
            row = db.get(AuditLog, row_id)
            if row is not None:
                db.delete(row)
