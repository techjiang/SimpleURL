"""全局配置：所有配置项均可通过环境变量覆盖。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _as_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return default


class Settings:
    """应用配置项。"""

    def __init__(self) -> None:
        base_dir = Path(__file__).resolve().parents[2]

        # 站点基础信息
        self.site_name: str = os.getenv("SITE_NAME", "SimpleURL").strip()
        self.host: str = os.getenv("HOST", "0.0.0.0").strip()
        self.port: int = _as_int("PORT", 8000)
        self.debug: bool = _as_bool("DEBUG", False)

        # 短码规则：默认 6 位，最短 4 位，最长 16 位
        self.code_length: int = max(4, min(16, _as_int("CODE_LENGTH", 6)))
        self.code_min_length: int = max(3, min(16, _as_int("CODE_MIN_LENGTH", 3)))
        self.code_max_length: int = max(8, min(32, _as_int("CODE_MAX_LENGTH", 16)))

        # 每个 IP 每天最多创建的链接数，防止滥用
        self.create_daily_limit: int = _as_int("CREATE_DAILY_LIMIT", 200)

        # 存储：默认 SQLite，可切换到 Postgres
        self.database_url: str = os.getenv("DATABASE_URL", "").strip() or f"sqlite:///{base_dir / 'simpleurl.db'}"

        # 访问模式：public（开放外部可使用）/ admin（仅管理员可使用）/ invite（邀请码可使用）
        raw_mode = os.getenv("ACCESS_MODE", "").strip().lower()
        legacy_admin = _as_bool("ADMIN_ONLY", False)
        if not raw_mode:
            raw_mode = "admin" if legacy_admin else "public"
        self.access_mode: str = raw_mode if raw_mode in ("public", "admin", "invite") else "public"

        # 管理密钥：留空则管理功能关闭，admin / invite 模式自动退回 public
        self.admin_token: str = os.getenv("ADMIN_TOKEN", "").strip()

        # 模板目录
        self.templates_dir = base_dir / 'app' / 'templates'
        self.static_dir = base_dir / 'app' / 'static'

        # 统计数据的采样间隔（秒），用于做简单的请求限速保护
        self.stats_rate_limit: int = _as_int("STATS_RATE_LIMIT", 1)

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_token)

    @property
    def effective_mode(self) -> str:
        """未配密钥时 admin / invite 会退化，这里给出实际生效的模式。"""
        if self.access_mode in ("admin", "invite") and not self.admin_enabled:
            return "public"
        return self.access_mode


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
