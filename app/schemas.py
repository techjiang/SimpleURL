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
    # 设了访问口令的短码，跳转前要先在验证页输入口令
    password: Optional[str] = Field(None, max_length=72)
    # 点击数上限，点满即失效；留空表示不限
    max_clicks: Optional[int] = Field(None, ge=1, le=1_000_000)

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

    @field_validator("password", mode="before")
    @classmethod
    def _v_password(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None


class LinkUpdate(BaseModel):
    """更新短链接。

    口令与点击数上限三态：字段不传＝不动，传 null＝清除，传值＝设置。
    用 `model_fields_set` 区分「没传」和「传了 null」，否则永远清不掉口令。
    """

    target: Optional[str] = None
    title: Optional[str] = None
    note: Optional[str] = None
    tags: Optional[list[str] | str] = None
    enabled: Optional[bool] = None
    expires_at: Optional[dt.datetime] = None
    set_password: Optional[str] = Field(None, max_length=72, description="设置新口令")
    clear_password: bool = Field(False, description="清除访问口令")
    clear_expiry: bool = Field(False, description="清除过期时间")
    max_clicks: Optional[int] = Field(None, ge=1, le=1_000_000)
    clear_max_clicks: bool = Field(False, description="取消点击上限")

    @field_validator("set_password", mode="before")
    @classmethod
    def _v_set_password(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None

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
    locked: bool = False
    max_clicks: Optional[int] = None
    exhausted: bool = False
    state: str = "active"


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


class MessageOut(BaseModel):
    ok: bool = True
    message: str
