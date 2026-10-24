"""数据模型。"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.core.timeutil import ensure_aware, iso, utcnow


class Link(Base):
    """短链接主表。"""

    __tablename__ = "links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON 数组字符串

    clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # 可选过期时间（UTC）
    expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    events: Mapped[list["ClickEvent"]] = relationship(
        back_populates="link", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_links_enabled_created", "enabled", "created_at"),
    )

    @property
    def is_expired(self) -> bool:
        expires = ensure_aware(self.expires_at)
        return bool(expires and expires <= utcnow())

    def to_dict(self) -> dict:
        tags = []
        if self.tags:
            import json

            try:
                tags = json.loads(self.tags)
            except (ValueError, TypeError):
                tags = []
        return {
            "id": self.id,
            "code": self.code,
            "target": self.target,
            "title": self.title,
            "note": self.note,
            "tags": tags,
            "clicks": self.clicks,
            "enabled": self.enabled,
            "expires_at": iso(self.expires_at),
            "expired": self.is_expired,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }


class ClickEvent(Base):
    """单次点击记录，用于统计图表。"""

    __tablename__ = "click_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    link_id: Mapped[int] = mapped_column(ForeignKey("links.id", ondelete="CASCADE"), index=True, nullable=False)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    referrer: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    ua: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    ip_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    link: Mapped[Link] = relationship(back_populates="events")

    __table_args__ = (
        Index("ix_click_events_link_ts", "link_id", "ts"),
    )


class SettingsRow(Base):
    """后台可改的运行期配置（键值对，值存 JSON 文本）。"""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ApiKey(Base):
    """API 密钥：只存哈希，明文仅在创建时回显一次。"""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)  # 便于人工辨认，非机密
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_used_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "prefix": self.prefix,
            "created_at": iso(self.created_at),
            "last_used_at": iso(self.last_used_at),
            "revoked": self.revoked,
        }


class GuardEvent(Base):
    """防洪留痕：被拦截或被 UA 屏蔽的请求，用来复盘攻击。"""

    __tablename__ = "guard_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)  # rate / ua / wechat / qq
    ua: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    ip_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("ix_guard_events_kind_ts", "kind", "ts"),
    )
