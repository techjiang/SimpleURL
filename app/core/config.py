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

        # 短码规则：默认 6 位，最短 3 位，最长 16 位
        self.code_length: int = max(3, min(16, _as_int("CODE_LENGTH", 6)))
        self.code_min_length: int = max(3, min(16, _as_int("CODE_MIN_LENGTH", 3)))
        self.code_max_length: int = max(8, min(32, _as_int("CODE_MAX_LENGTH", 16)))

        # 每个 IP 每天最多创建的链接数，防止滥用
        self.create_daily_limit: int = _as_int("CREATE_DAILY_LIMIT", 200)

        # 存储：默认 SQLite，可切换到 Postgres
        self.database_url: str = os.getenv("DATABASE_URL", "").strip() or f"sqlite:///{base_dir / 'simpleurl.db'}"

        # 管理后台（留空表示关闭后台登录）
        self.admin_token: str = os.getenv("ADMIN_TOKEN", "").strip()
        # 会话签名密钥，默认复用管理令牌
        self.session_secret: str = os.getenv("SECRET_KEY", "").strip() or self.admin_token
        # 后台登录态有效期（秒）
        self.session_ttl: int = max(300, _as_int("ADMIN_SESSION_TTL", 43200))

        # 访问短链是否需要携带访问令牌（留空表示公开）
        self.access_token: str = os.getenv("ACCESS_TOKEN", "").strip()

        # 模板目录
        self.templates_dir = base_dir / 'app' / 'templates'
        self.static_dir = base_dir / 'app' / 'static'

        # 统计数据的采样间隔（秒），用于做简单的请求限速保护
        self.stats_rate_limit: int = _as_int("STATS_RATE_LIMIT", 1)

        # ---------- 反向代理 ----------
        # 信任哪些代理的 X-Forwarded-For / X-Real-IP。默认只信任回环地址，
        # 直连部署时等于不信任任何转发头；填 * 表示无条件信任（仅限内网可信环境）。
        self.trusted_proxies: str = os.getenv("TRUSTED_PROXIES", "127.0.0.1,::1").strip()

        # ---------- 登录防爆破 ----------
        # 单 IP 5 分钟内的后台登录尝试上限，0 不限制
        self.login_rate_limit: int = max(0, _as_int("LOGIN_RATE_LIMIT", 10))

        # 后台口令最短长度：短口令等于没设防，启动时给个下限
        self.min_admin_token_length: int = max(0, _as_int("MIN_ADMIN_TOKEN_LENGTH", 12))

        # 每页展示条数的可选值
        self.page_sizes: list[int] = [10, 20, 50, 100]

        # 首页单次最多导出的条数
        self.export_max_rows: int = max(1, min(100000, _as_int("EXPORT_MAX_ROWS", 50000)))

        # ---------- 防洪 ----------
        # 单 IP 每分钟跳转上限，0 表示不限制（可在后台改）
        self.redirect_rate_limit: int = _as_int("REDIRECT_RATE_LIMIT", 120)
        # 单 IP 每日点击上限，0 表示不限制（可在后台改）
        self.click_daily_limit: int = _as_int("CLICK_DAILY_LIMIT", 0)

        # ---------- 社交平台防洪 ----------
        # 微信 / QQ 等内置浏览器可能被平台风控，这里给一套可配置的放行/计数/拦截策略，
        # 后台可逐平台调，改动落库即时生效
        self.social_guard: bool = _as_bool("SOCIAL_GUARD", True)
        self.social_block_hint: str = os.getenv(
            "SOCIAL_BLOCK_HINT",
            "该链接暂时无法在此处打开，请点击右上角选择「在浏览器中打开」，或复制链接后重试。",
        ).strip()
        self.social_tag_wechat: bool = _as_bool("SOCIAL_TAG_WECHAT", True)
        self.social_tag_qq: bool = _as_bool("SOCIAL_TAG_QQ", True)

        # ---------- 延迟计数 ----------
        # 点击事件缓冲多久批量落库一次（秒）
        self.click_flush_interval: int = max(1, min(600, _as_int("CLICK_FLUSH_INTERVAL", 5)))
        # 缓冲队列上限，超出后直接丢弃最旧的保护策略由 drop 计数体现
        self.click_buffer_max: int = max(100, min(1000000, _as_int("CLICK_BUFFER_MAX", 20000)))

        # ---------- 自动获取标题 ----------
        self.fetch_title: bool = _as_bool("FETCH_TITLE", True)
        self.fetch_title_timeout: int = max(1, min(30, _as_int("FETCH_TITLE_TIMEOUT", 5)))

        # ---------- API 密钥 ----------
        # 开启后 /api/* 必须携带 X-Api-Key 或 ?key=
        self.require_api_key: bool = _as_bool("REQUIRE_API_KEY", False)

        # YOURLS 兼容接口的签名令牌：只有显式配置才校验，不默认继承管理令牌，
        # 否则配了后台密码就等于把兼容接口也锁了，违背"开箱即用"
        self.yourls_token: str = os.getenv("YOURLS_TOKEN", "").strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
