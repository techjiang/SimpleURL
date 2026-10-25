"""通知通道：Webhook / 邮件 / Bark。

这东西的价值不在「多个功能」，在于**告警不再只能靠人盯后台**。
之前的部署自检、配额告警、到期提醒全都写在页面上，得有人主动点开运行状态才看得见；
真出事的那天，往往谁都没在看。所以这里做的最小可用版本是「把已有告警送出去」。

设计上刻意保守：

- 后台线程发送，请求不被外部服务拖住（一个 5 秒超时的 webhook 不该让保存变卡）
- 每次发送留一条结果（成功/失败/原因），面板上直接看，不靠翻日志
- webhook 地址走同一套 SSRF 校验，避免被用来探内网
- 邮件只在配了 SMTP 时才出现，没配就是一行说明，不是坏掉的功能
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import smtplib
import ssl
import threading
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import fmt_local, utcnow
from app.models import Setting

logger = logging.getLogger("simpleurl")

CHANNELS = {
    "webhook": "Webhook（JSON POST）",
    "email": "邮件（SMTP）",
    "bark": "Bark（iOS 推送）",
}

# 每种通道的可配置项：键 -> (默认值, 说明)
OPTIONS: dict[str, dict[str, tuple[str, str]]] = {
    "webhook": {
        "url": ("", "接收 JSON 的地址，形如 https://example.com/hook"),
        "secret": ("", "可选。填了会在请求头带 X-SimpleURL-Secret，方便对端验身"),
    },
    "email": {
        "host": ("", "SMTP 服务器，如 smtp.example.com:465"),
        "user": ("", "登录账号"),
        "password": ("", "登录密码或授权码"),
        "to": ("", "收件人，多个用逗号分隔"),
        "starttls": ("1", "465 端口用 SSL，587 用 STARTTLS；这里填 1 表示 STARTTLS"),
        "from": ("", "发件人，留空用登录账号"),
    },
    "bark": {
        "url": ("", "形如 https://api.day.app/你的Key"),
    },
}

OPTION_PREFIX = "notify_"


def _opt_key(channel: str, name: str) -> str:
    return f"{OPTION_PREFIX}{channel}_{name}"


def options(db: Session, channel: str) -> dict[str, str]:
    """通道配置：库里优先，环境变量兜底，都没有就用默认值。"""
    out: dict[str, str] = {}
    stored = {
        row.key: row.value
        for row in db.execute(select(Setting).where(Setting.key.like(f"{OPTION_PREFIX}%"))).scalars().all()
    }
    for name, (default, _hint) in OPTIONS.get(channel, {}).items():
        key = _opt_key(channel, name)
        env_name = f"NOTIFY_{channel.upper()}_{name.upper()}"
        value = stored.get(key)
        if value is None:
            value = getattr(settings, f"notify_{channel}_{name}", "") or default
        out[name] = str(value)
    return out


def save_options(db: Session, channel: str, values: dict) -> None:
    for name in OPTIONS.get(channel, {}):
        if name not in values:
            continue
        key = _opt_key(channel, name)
        row = db.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
        text = str(values.get(name) or "")
        if row is None:
            db.add(Setting(key=key, value=text))
        else:
            row.value = text
    db.flush()


def enabled(db: Session) -> list[str]:
    """哪些通道齐了必要配置。齐了才在界面上给「发一条测试」。"""
    out = []
    if options(db, "webhook")["url"].strip():
        out.append("webhook")
    if options(db, "bark")["url"].strip():
        out.append("bark")
    mail = options(db, "email")
    if mail["host"].strip() and mail["to"].strip():
        out.append("email")
    return out


# ---------- 发送 ----------


def _run_async(func, *args) -> None:
    """后台线程发送：外部服务慢，不该让调它的请求跟着慢。"""
    thread = threading.Thread(target=func, args=args, daemon=True)
    thread.start()


def _deliver(channel: str, db_options: dict[str, str], title: str, body: str, level: str) -> tuple[bool, str]:
    from app.core.security import is_public_url

    if channel == "webhook":
        url = db_options.get("url", "").strip()
        if not url:
            return False, "未配置 webhook 地址"
        if not is_public_url(url):
            return False, "webhook 地址不合法（仅支持公开 http/https）"
        import httpx

        headers = {"Content-Type": "application/json"}
        secret = db_options.get("secret", "").strip()
        if secret:
            headers["X-SimpleURL-Secret"] = secret
        payload = {
            "title": title,
            "text": body,
            "level": level,
            "site": settings.site_name,
            "time": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        resp = httpx.post(url, json=payload, headers=headers, timeout=8)
        if resp.status_code >= 400:
            return False, f"对方返回 {resp.status_code}"
        return True, f"已投递（{resp.status_code}）"

    if channel == "bark":
        url = db_options.get("url", "").rstrip("/")
        if not url:
            return False, "未配置 Bark 地址"
        if not is_public_url(url):
            return False, "Bark 地址不合法"
        import httpx

        if url.startswith("https://api.day.app/"):
            # 官方服务走路径式，直接把内容接在后面
            target = f"{url}/{_quote(title)}/{_quote(body)}?level={'timeSensitive' if level == 'warn' else 'active'}"
            resp = httpx.get(target, timeout=8)
        else:
            resp = httpx.post(
                url,
                json={"title": title, "body": body, "level": "timeSensitive" if level == "warn" else "active"},
                timeout=8,
            )
        if resp.status_code >= 400:
            return False, f"Bark 返回 {resp.status_code}"
        return True, "已推送"

    if channel == "email":
        host = db_options.get("host", "").strip()
        to = db_options.get("to", "").strip()
        if not host or not to:
            return False, "未配置 SMTP 服务器或收件人"
        user = db_options.get("user", "").strip()
        password = db_options.get("password", "")
        sender = db_options.get("from", "").strip() or user
        port = 465
        if ":" in host:
            host, port_text = host.rsplit(":", 1)
            try:
                port = int(port_text)
            except ValueError:
                port = 465
        message = MIMEText(body, "plain", "utf-8")
        message["Subject"] = Header(title, "utf-8")
        message["From"] = formataddr((str(Header(settings.site_name, "utf-8")), sender or user or "simpleurl@localhost"))
        message["To"] = to
        recipients = [p.strip() for p in to.replace("，", ",").split(",") if p.strip()]
        use_starttls = str(db_options.get("starttls", "1")).strip().lower() in {"1", "true", "yes", "on"}
        context = ssl.create_default_context()
        if use_starttls:
            with smtplib.SMTP(host, port, timeout=10) as client:
                client.ehlo()
                client.starttls(context=context)
                if user:
                    client.login(user, password)
                client.sendmail(sender or user, recipients, message.as_string())
        else:
            with smtplib.SMTP_SSL(host, port, timeout=10, context=context) as client:
                if user:
                    client.login(user, password)
                client.sendmail(sender or user, recipients, message.as_string())
        return True, f"已发送给 {len(recipients)} 人"

    return False, f"未知通道 {channel}"


def _quote(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


def send(db: Session, channel: str, title: str, body: str, level: str = "info") -> dict:
    """同步发一条，结果写进通道状态。测试按钮用它，告警线程也用它。

    失败不抛异常：告警通道自己挂了，不该把调用方（比如保存设置）一起带崩。
    """
    opts = options(db, channel)
    try:
        ok, message = _deliver(channel, opts, title, body, level)
    except Exception as exc:  # noqa: BLE001 - 外部服务的任何异常都只记结果
        logger.warning("通知通道 %s 发送失败：%s", channel, exc)
        ok, message = False, f"{type(exc).__name__}: {exc}"[:160]
    record(db, channel, ok, message, title)
    return {"ok": ok, "message": message, "channel": channel}


def send_async(channel: str, title: str, body: str, level: str = "warn") -> None:
    """异步版：告警线程里用，自己开 session 自己关。"""
    from app.core.db import session_scope

    def _worker() -> None:
        try:
            with session_scope() as db:
                send(db, channel, title, body, level)
        except Exception:  # noqa: BLE001
            logger.exception("异步通知失败")

    _run_async(_worker)


def broadcast(db: Session, title: str, body: str, level: str = "warn") -> list[dict]:
    """把一条告警送到所有配好的通道。"""
    results = []
    for channel in enabled(db):
        results.append(send(db, channel, title, body, level))
    return results


# ---------- 状态留痕 ----------


def record(db: Session, channel: str, ok: bool, message: str, title: str = "") -> None:
    """最近一次发送结果。只留一行/通道，不做发送历史表——那该在日志里。"""
    now = fmt_local(utcnow(), "%Y-%m-%d %H:%M:%S")
    pairs = {
        _opt_key(channel, "last_ok"): "1" if ok else "0",
        _opt_key(channel, "last_message"): message[:200],
        _opt_key(channel, "last_title"): title[:120],
        _opt_key(channel, "last_at"): now,
    }
    for key, value in pairs.items():
        row = db.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
        if row is None:
            db.add(Setting(key=key, value=value))
        else:
            row.value = value
    db.flush()


def state(db: Session) -> dict[str, dict]:
    """每个通道的配置与最近一次结果，面板直接渲染。"""
    stored = {
        row.key: row.value
        for row in db.execute(select(Setting).where(Setting.key.like(f"{OPTION_PREFIX}%"))).scalars().all()
    }
    out = {}
    for key, label in CHANNELS.items():
        opts = options(db, key)
        out[key] = {
            "key": key,
            "label": label,
            "configured": key in enabled(db),
            "options": {k: ("" if k in {"password", "secret"} else v) for k, v in opts.items()},
            "has_password": bool(opts.get("password")),
            "has_secret": bool(opts.get("secret")),
            "last_ok": stored.get(_opt_key(key, "last_ok")),
            "last_message": stored.get(_opt_key(key, "last_message"), ""),
            "last_title": stored.get(_opt_key(key, "last_title"), ""),
            "last_at": stored.get(_opt_key(key, "last_at"), ""),
            "hint": OPTIONS[key],
        }
    return out


def digest_body(warnings: list[dict], db: Session) -> str:
    """把告警拼成一段人话。别发 JSON——收件人是在手机上看的。"""
    lines = [f"站点：{settings.site_name}"]
    if warnings:
        lines.append("")
        for item in warnings[:12]:
            lines.append(f"· {item.get('text', '')}")
        if len(warnings) > 12:
            lines.append(f"…… 另有 {len(warnings) - 12} 条")
    else:
        lines.append("")
        lines.append("没有新的告警。")
    lines.append("")
    lines.append("这条消息来自 SimpleURL 的通知通道。")
    return "\n".join(lines)
