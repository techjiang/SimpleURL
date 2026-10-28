"""数据模型。"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.core.timeutil import ensure_aware, iso, today_local, utcnow

# 主键在 SQLite 上必须是字面的 `INTEGER` 才等于 rowid，只有它才会跟着插入自动分配号
# （写成 BIGINT 就只是个普通列，插入直接 NOT NULL 报错）。收成一个常量是为了
# 别在五个模型里各写一遍 —— 也提醒后来人：这里别乱换类型。
AUTO_ID = Integer


class Link(Base):
    """短链接主表。"""

    __tablename__ = "links"

    id: Mapped[int] = mapped_column(AUTO_ID, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON 数组字符串

    clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # 可选过期时间（UTC）
    expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # 一次性领取码的哈希：开放模式下访客凭它回看自己建的那条链接
    claim_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # 这条链接自己的每日点击上限，0 表示不限制。
    # 站点级的 click_daily_limit 是「单 IP 防刷」，这里是「活动预算」——
    # 限量兑换、内测名额这类场景要的是「这条链接一共只放 N 个人进来」，
    # 跟谁点的没关系。所以单独一套账，不与防刷配额混用。
    daily_limit: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    # 当日已用次数与所属日期（UTC 日期）。日期一变即视为 0，
    # 不靠定时任务清零：多实例下定时任务会重复跑，漏一次就永久卡死。
    daily_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    daily_date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)

    # 删除时间：有值即进了回收站。软删除而不是直接 DELETE，
    # 因为「手滑删了一条发出去半年的短链」是不可逆事故——回收站留 30 天。
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # 标题来源：fetched 抓回来的 / fallback URL 兜底 / manual 人工填的
    # 分不清这三者，后台就没法回答「自动取标题到底有没有在工作」
    title_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    # `lazy="selectin"` 是这里最大的一个隐患：只要有一处 Link 查询，
    # SQLAlchemy 就会顺手把每条链接的**全部**点击明细再查一遍。
    # 一页 100 条热链接 = 十几万行明细进内存，然后被丢掉——没有一处在用。
    # 改成按需加载：真要明细就走显式查询（统计页自己聚合）。
    events: Mapped[list["ClickEvent"]] = relationship(
        back_populates="link", cascade="all, delete-orphan", lazy="select"
    )

    __table_args__ = (
        Index("ix_links_enabled_created", "enabled", "created_at"),
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def has_daily_limit(self) -> bool:
        return int(self.daily_limit or 0) > 0

    @property
    def daily_used_today(self) -> int:
        """今天的已用次数。日期不是今天就算 0——日切靠比对，不靠定时清零。

        这里比的必须是**本地**自然日：页面写的是「明天（零点）恢复」。
        按 UTC 切的话，东八区用户的额度会在早上 8 点忽然回满，
        而文案说的是零点——两头对不上，比不限额更让人困惑。
        """
        if self.daily_date is None or self.daily_date != today_local():
            return 0
        return int(self.daily_used or 0)

    @property
    def daily_exhausted(self) -> bool:
        return self.has_daily_limit and self.daily_used_today >= int(self.daily_limit)

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
            "daily_limit": int(self.daily_limit or 0),
            "daily_used": self.daily_used_today,
            "daily_exhausted": self.daily_exhausted,
            "expires_at": iso(self.expires_at),
            "expired": self.is_expired,
            "title_source": self.title_source,
            "deleted_at": iso(self.deleted_at),
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }


class ClickEvent(Base):
    """单次点击记录，用于统计图表。"""

    __tablename__ = "click_events"

    id: Mapped[int] = mapped_column(AUTO_ID, primary_key=True, autoincrement=True)
    link_id: Mapped[int] = mapped_column(ForeignKey("links.id", ondelete="CASCADE"), index=True, nullable=False)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    referrer: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    ua: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    device: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    ip_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    # 是否被 UA / 社交平台防洪规则拦下（拦下也记一条，方便后台看战果）
    blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default='0')
    # 访问来源平台：wechat / wecom / qq / weibo ...，认不出为空
    platform: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
    # 这一条为什么没算进点击数：ua（UA 黑名单）/ social（社交拦截）/
    # social_silent（放行不计数）/ quota（限速或每日配额）。有值即等于 blocked。
    #
    # 原来这些情况全塞在 blocked 一个布尔里，后台只能显示「已拦截」——
    # 可「微信里被拦下」和「每人每天第 N 次之后不再计数」是两件事：
    # 前者要看战果，后者往往是配额配小了，得把数字调大。
    block_reason: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    link: Mapped[Link] = relationship(back_populates="events")

    __table_args__ = (
        Index("ix_click_events_link_ts", "link_id", "ts"),
    )


class LinkVisitor(Base):
    """某条链接在某个本地自然日来过谁（按 IP 摘要去重）。

    「独立访客」原来是查 click_events 算 `count(distinct ip_hash)`。可明细
    按保留期清理（默认 180 天，见 `prune_click_events`），清完之后这个数字
    会跟着往下掉——总点击不变、独立访客缩水，同一个页面上两个数字各说各话。

    所以单开一张小表把「谁来过」留住：一行 = (链接, 日期, 摘要)，明细清掉
    也不影响累计。写入跟着延迟计数那一批走（`ClickBuffer._add_visitors`），
    唯一键让重复写入直接跳过，并发下不会写重。
    """

    __tablename__ = "link_visitors"

    id: Mapped[int] = mapped_column(AUTO_ID, primary_key=True, autoincrement=True)
    link_id: Mapped[int] = mapped_column(ForeignKey("links.id", ondelete="CASCADE"), index=True, nullable=False)
    day: Mapped[dt.date] = mapped_column(Date, nullable=False)
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("link_id", "day", "ip_hash", name="uq_link_visitors"),
    )


class ClaimRecord(Base):
    """回执登记：让丢了一次性回执的访客还能找回来。

    回执（领取码）服务端只存哈希，丢了就真没了——开放模式下又没有管理员可问。
    以前的办法是「拿旧回执换新回执」，可也常常连旧的都不在手上了。

    折中方案：创建时可以让访客留一个 **线索**（邮箱或一句自述），线索不加密
    但也救不了任何人的链接——反查时必须同时提供线索，而且同一线索 1 小时
    内只为最近的一条链接重签回执，且只有 3 次机会。这样即便有人知道你的邮箱，
    他也拿不到「所有」链接，只能碰运气看到最新那条有没有人动过。
    """

    __tablename__ = "claim_records"

    id: Mapped[int] = mapped_column(AUTO_ID, primary_key=True, autoincrement=True)
    link_id: Mapped[int] = mapped_column(ForeignKey("links.id", ondelete="CASCADE"), index=True, nullable=False)
    # 线索的哈希：邮箱本身不落库，只存归一化后的 SHA-256
    clue_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_claim_records_link", "link_id"),
    )


class AuditLog(Base):
    """操作留痕：谁在什么时候对哪条链接做了什么。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(AUTO_ID, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="匿名")
    code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ip_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class Setting(Base):
    """键值配置表：存可在后台改的运行参数。"""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ApiKey(Base):
    """API 访问密钥。"""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(AUTO_ID, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="默认密钥")
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_used_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "key": self.key,
            "enabled": self.enabled,
            "created_at": iso(self.created_at),
            "last_used_at": iso(self.last_used_at),
        }
