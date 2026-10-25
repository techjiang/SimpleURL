"""API 数据模型（Pydantic）。"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _clean_tags(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        parts = [p.strip() for p in v.replace("，", ",").split(",")]
    else:
        parts = [str(p).strip() for p in v]
    out, seen = [], set()
    for p in parts:
        if not p or len(p) > 32:
            continue
        if p.lower() in seen:
            continue
        seen.add(p.lower())
        out.append(p)
    return out[:10]


class LinkCreate(BaseModel):
    """创建短链接的请求体。"""

    target: str = Field(..., description="目标地址")
    code: Optional[str] = Field(None, description="自定义短码，留空则自动生成")
    title: Optional[str] = Field(None, max_length=255)
    note: Optional[str] = Field(None, max_length=1000)
    tags: Optional[list[str] | str] = Field(default=None)
    expires_at: Optional[dt.datetime] = None
    # 传 false 表示这次跳过自动取标题（批量导入、脚本里不想等出网时用）
    fetch_title: Optional[bool] = Field(None, description="本次是否自动获取标题，留空按站点设置")

    @field_validator("target")
    @classmethod
    def _v_target(cls, v: str) -> str:
        from app.core.security import normalize_url

        v = normalize_url(v)
        if not v:
            raise ValueError("目标地址不能为空")
        return v

    @field_validator("title", "note", mode="before")
    @classmethod
    def _v_text(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    @field_validator("tags", mode="before")
    @classmethod
    def _v_tags(cls, v):
        return _clean_tags(v)

    @field_validator("code", mode="before")
    @classmethod
    def _v_code(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None


class LinkUpdate(BaseModel):
    """更新短链接。"""

    target: Optional[str] = None
    title: Optional[str] = None
    note: Optional[str] = None
    tags: Optional[list[str] | str] = None
    enabled: Optional[bool] = None
    expires_at: Optional[dt.datetime] = None

    @field_validator("target")
    @classmethod
    def _v_target(cls, v):
        from app.core.security import normalize_url

        return normalize_url(v) if v else v

    @field_validator("title", "note", mode="before")
    @classmethod
    def _v_text(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    @field_validator("tags", mode="before")
    @classmethod
    def _v_tags(cls, v):
        return _clean_tags(v)


class LinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    target: str
    title: Optional[str] = None
    note: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    clicks: int
    enabled: bool
    expires_at: Optional[dt.datetime] = None
    created_at: Optional[dt.datetime] = None
    updated_at: Optional[dt.datetime] = None
    short_url: str = ""
    expired: bool = False
    # 标题来源：fetched 抓回来的、fallback URL 兜底、off 没开或没填
    title_source: Optional[str] = None
    # 创建者手里的一次性回执：凭它回看这一条（仅创建响应里返回一次）
    claim_token: Optional[str] = None
    stats_url: Optional[str] = None


class LinkListOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[LinkOut]


class StatsOut(BaseModel):
    code: str
    clicks: int
    unique_visitors: int
    created_at: Optional[dt.datetime] = None
    last_click_at: Optional[dt.datetime] = None
    daily: list[dict] = Field(default_factory=list)
    top_referrers: list[dict] = Field(default_factory=list)
    devices: list[dict] = Field(default_factory=list)
    # 每项含 platform / key / count / color / icon（墨迹色与图标路径）
    platforms: list[dict] = Field(default_factory=list)
    blocked: int = 0
    # 其中属于「放行不计数」的部分（用户其实打开了页面）
    silent: int = 0


class MessageOut(BaseModel):
    ok: bool = True
    message: str


class SettingsOut(BaseModel):
    """后台可见的生效配置。"""

    site_name: str
    redirect_rate_limit: int
    click_daily_limit: int
    fetch_title: bool
    require_api_key: bool
    create_daily_limit: int
    code_length: int = 4
    click_flush_interval: int
    pending_clicks: int
    admin_enabled: bool
    access_required: bool
    access_mode: str = "open"
    access_mode_label: str = "开放外部可使用"
    access_locked: bool = False
    announcement: str = ""
    site_tagline: str = ""
    # 社交平台防洪
    social_guard: bool = True
    social_block_hint: str = ""
    social_tag_qq: bool = True
    social_tag_wechat: bool = True


class SettingsUpdate(BaseModel):
    site_name: Optional[str] = None
    redirect_rate_limit: Optional[int] = None
    click_daily_limit: Optional[int] = None
    fetch_title: Optional[bool] = None
    require_api_key: Optional[bool] = None
    create_daily_limit: Optional[int] = None
    code_length: Optional[int] = None
    access_mode: Optional[str] = None
    announcement: Optional[str] = Field(None, max_length=280)
    site_tagline: Optional[str] = Field(None, max_length=120)
    social_guard: Optional[bool] = None
    social_block_hint: Optional[str] = None
    social_tag_qq: Optional[bool] = None
    social_tag_wechat: Optional[bool] = None

    @field_validator("access_mode", mode="before")
    @classmethod
    def _v_access_mode(cls, v):
        from app.core.access import normalize_mode

        return normalize_mode(v) if v is not None else None


class UaRuleOut(BaseModel):
    ok: bool = True
    rules: list[str] = Field(default_factory=list)


class PlatformRuleOut(BaseModel):
    """社交平台防洪：策略表 + 拦截页提示语。"""

    ok: bool = True
    rules: dict[str, str] = Field(default_factory=dict)
    hint: str = ""
    platforms: list[dict] = Field(default_factory=list)


class ApiKeyOut(BaseModel):
    id: int
    name: str
    key: str
    enabled: bool
    created_at: Optional[dt.datetime] = None
    last_used_at: Optional[dt.datetime] = None


class ApiKeyCreate(BaseModel):
    name: Optional[str] = None

    @field_validator("name", mode="before")
    @classmethod
    def _v_name(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return v[:64] or None


class MigrationOut(BaseModel):
    ok: bool = True
    imported: int
    renamed: int
    failed: int
    total: int


class MigrationIn(BaseModel):
    payload: str = Field(..., description="YOURLS 的 SQL 转储或 JSON 导出文本")


class AdminLogin(BaseModel):
    username: str
    password: str


class DashboardOut(BaseModel):
    total_links: int
    total_clicks: int
    enabled_links: int
    disabled_links: int
    expired_links: int
    # 7 天内到期：到点了才发现链接失效，那叫事故
    expiring_links: int = 0
    expiring: list[dict] = Field(default_factory=list)
    pending_clicks: int
    # blocked_events 是所有「被留痕但没计数」的事件总数，其中真实点击多少、
    # 拦截留痕多少要拆开说——把拦截算进总点击，数字就跟链接列表对不上
    blocked_events: int
    clicks_today: int
    total_events: int = 0
    top_links: list[dict] = Field(default_factory=list)
    recent_clicks: list[dict] = Field(default_factory=list)
    daily: list[dict] = Field(default_factory=list)
