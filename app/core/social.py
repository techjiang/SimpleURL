"""社交平台防洪：微信 / QQ / 微博 / 支付宝等内置信使的识别与策略。

短链在国内最常见的传播路径就是微信、QQ 里点开，而这两个平台的客户端
UA 里带着特征标记（MicroMessenger、QQ/、QQBrowser 等）。这里把「是谁在访问」
单独抽出来，交给防洪策略决定：照常跳转、打标不计数，还是干脆拦下。

被拦下的访问会返回一张带品牌的中转页（wxblock.html），可以配提示语和跳转
目标——微信里外链被举报后，直接 403 白屏是最难看的结局，给一页说明总比白屏好。
"""

from __future__ import annotations

# 平台键 -> (中文名, UA 特征标记，按序匹配)
PLATFORMS: dict[str, tuple[str, tuple[str, ...]]] = {
    "wechat": ("微信", ("micromessenger",)),
    "wecom": ("企业微信", ("wxwork",)),
    "qq": ("QQ", ("qq/", "qqbrowser", "mqqbrowser")),
    "weibo": ("微博", ("weibo",)),
    "alipay": ("支付宝", ("alipayclient", "alipay")),
    "dingtalk": ("钉钉", ("dingtalk",)),
    "douyin": ("抖音", ("aweme", "bytedance")),
    "xiaohongshu": ("小红书", ("xhs", "discover")),
}

# 策略：
#   allow  照常跳转并计数（默认）
#   silent 照常跳转，但打标记不计数（防刷量的同时不影响真实用户）
#   block  拦下，返回中转页
STRATEGIES = {
    "allow": "放行并计数",
    "silent": "放行不计数",
    "block": "拦截并提示",
}
DEFAULT_STRATEGY = "allow"

# 常见来源落地页 -> 平台（微信/QQ 里经常拿不到 Referer，UA 也认不出时靠它兜底）
_PLATFORM_TOKENS = tuple(
    (name, (marker, *marks)) for name, (marker, marks) in PLATFORMS.items()
)


def detect(ua: str | None, referrer: str | None = None) -> str | None:
    """识别访问来源平台，认不出返回 None。"""
    raw = (ua or "").lower()
    # 顺序有讲究：企业微信的 UA 同时带 MicroMessenger，先认企业微信
    for key in ("wecom", "wechat", "qq", "dingtalk", "weibo", "alipay", "douyin", "xiaohongshu"):
        _, marks = PLATFORMS[key]
        if any(m in raw for m in marks):
            return key
    if referrer:
        ref = referrer.lower()
        return platform_of_host(ref)
    return None


def platform_of_host(text: str) -> str | None:
    if not text:
        return None
    mapping = (
        ("wechat", ("weixin.qq.com", "mp.weixin.qq.com", "servicewechat.com")),
        ("wecom", ("work.weixin.qq.com",)),
        ("qq", ("qq.com", "qzone.qq.com", "im.qq.com")),
        ("weibo", ("weibo.com", "weibo.cn")),
        ("alipay", ("alipay.com",)),
        ("douyin", ("douyin.com", "iesdouyin.com")),
        ("xiaohongshu", ("xiaohongshu.com", "xhslink.com")),
    )
    for key, hosts in mapping:
        if any(h in text for h in hosts):
            return key
    return None


def label(key: str | None) -> str:
    if not key:
        return "其它来源"
    return PLATFORMS.get(key, (key, ()))[0]


def known(key: str) -> bool:
    return key in PLATFORMS


def normalize_strategy(raw: object) -> str:
    value = str(raw or "").strip().lower()
    return value if value in STRATEGIES else DEFAULT_STRATEGY


def strategies_from_text(text: str | None) -> dict[str, str]:
    """解析「wechat=block」这样的逐行配置。"""
    out: dict[str, str] = {}
    if not text:
        return out
    for line in str(text).replace(",", "\n").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if known(key):
            out[key] = normalize_strategy(value)
    return out


def strategies_to_text(rules: dict[str, str]) -> str:
    return "\n".join(f"{k}={v}" for k, v in rules.items() if known(k))
