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

# Referer 里出现这些片段就认作对应平台
REFERRER_TOKENS: dict[str, tuple[str, ...]] = {
    "wechat": ("weixin.qq.com", "servicewechat.com"),
    "qq": ("url.cn", "qurl.cn", "qzone.qq.com", "im.qq.com"),
}

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
        hit = platform_of_host(ref)
        if hit:
            return hit
        # 最后一道：微信 / QQ 的「复制链接」会在 Referer 里带上自家域名，
        # 认域名比猜 UA 稳，也顺手覆盖了从聊天窗口外链打开的情况
        for key in ("wechat", "qq"):
            for token in REFERRER_TOKENS.get(key, ()):
                if token in ref:
                    return key
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


# 每个平台的墨迹色：官网主题色，前端画色点用，免得一屏灰点看不出是谁
COLORS: dict[str, str] = {
    "wechat": "#07c160",
    "wecom": "#2f7bff",
    "qq": "#12b7f5",
    "weibo": "#e6162d",
    "alipay": "#1677ff",
    "dingtalk": "#3296fa",
    "douyin": "#fe2c55",
    "xiaohongshu": "#ff2442",
}

# 图标路径：全部使用 24×24 视口下的极简轮廓，前端拼成 <svg><path d="..."/></svg>。
# 不用 emoji、不引外链，就是两三个字母的自绘标记——平台名也要印得出来。
ICONS: dict[str, str] = {
    "wechat": "M4 10.5C4 7 7.6 4.2 12 4.2s8 2.8 8 6.3-3.6 6.3-8 6.3c-.9 0-1.8-.1-2.6-.3L6 18l.7-2.6C5 14.3 4 12.5 4 10.5Z",
    "wecom": "M4 6.5h7.2v5.2H4zM12.8 6.5H20V12h-7.2zM4 13.6h7.2V19H4zM12.8 13.6H20V19h-7.2z",
    "qq": "M12 3.6c2.6 0 4.4 1.9 4.4 4.4 0 .8.5 1.3 1 2.2.7 1.2 1.1 2.6 1.1 3.9 0 .9-.4 1.5-1 1.5-.7 0-1.2-.8-1.5-1.6-.5 1.6-2.1 3.4-4 3.4s-3.5-1.8-4-3.4c-.3.8-.8 1.6-1.5 1.6-.6 0-1-.6-1-1.5 0-1.3.4-2.7 1.1-3.9.5-.9 1-1.4 1-2.2C7.6 5.5 9.4 3.6 12 3.6Z",
    "weibo": "M10.5 6.2c3 0 5.4 1.9 5.4 4.3 0 2.4-2.4 4.3-5.4 4.3S5.1 12.9 5.1 10.5c0-2.4 2.4-4.3 5.4-4.3Zm5.6-2.4c2.1 0 3.8 1.7 3.8 3.8M18.4 6.5c.8 0 1.4.6 1.4 1.4",
    "alipay": "M7 6.8h10M12 4v5.6c0 3.2-2 6.1-5 7.6M4.6 15.2c1.8-1.6 4.6-2.4 7.2-2.4 2.2 0 4.3.5 5.8 1.4 1.3.8 2 1.8 1.7 2.6-.4 1.2-2.6 1.6-4.6 1-2-.5-3.9-1.8-5.1-3.3",
    "dingtalk": "M12 3.8 5.6 8v8L12 20.2 18.4 16V8L12 3.8Zm0 6.4a1.8 1.8 0 1 1 0 3.6 1.8 1.8 0 0 1 0-3.6Z",
    "douyin": "M14.4 4c.3 1.8 1.5 3.1 3.3 3.4v2.4c-1.3 0-2.4-.4-3.3-1v4.6c0 2.5-1.9 4.4-4.3 4.4S5.8 15.9 5.8 13.4s1.9-4.4 4.3-4.4c.3 0 .5 0 .8.1v2.4a2 2 0 1 0 1.4 1.9V4h2.1Z",
    "xiaohongshu": "M6.4 5h11.2A1.4 1.4 0 0 1 19 6.4v11.2a1.4 1.4 0 0 1-1.4 1.4H6.4A1.4 1.4 0 0 1 5 17.6V6.4A1.4 1.4 0 0 1 6.4 5Zm3 4.2v5.6M9.4 12h3l2.6-2.8M12.4 12l2.6 2.8",
}


def color(key: str | None) -> str:
    """平台墨迹色，认不出给中性灰（前端拿它当 CSS 颜色值用）。"""
    return COLORS.get(key or "", "#7c889c")


def icon(key: str | None) -> str:
    """平台图标的 SVG path；认不出的平台不给路径，前端退回色点。"""
    return ICONS.get(key or "", "")
