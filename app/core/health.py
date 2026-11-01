"""运行信息：版本、运行时长、进程与出网能力。

「服务活着吗」和「服务还能干活吗」是两个问题。这里的每一项都是可验证的事实，
不靠猜：解析一次 DNS 就知道出不出得了网，读一次 procfs 就知道内存还剩多少。
"""

from __future__ import annotations

import os
import platform
import sys
import time
from pathlib import Path

from app import __version__

STARTED_AT = time.time()


def uptime() -> int:
    return int(max(0, time.time() - STARTED_AT))


def human_duration(seconds: int) -> str:
    days, rest = divmod(max(0, int(seconds)), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}天 {hours}小时"
    if hours:
        return f"{hours}小时 {minutes}分"
    if minutes:
        return f"{minutes}分 {secs}秒"
    return f"{secs}秒"


def human_bytes(size: int | None) -> str:
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def memory() -> dict:
    """当前进程的内存与线程数。

    读 /proc/self/status 而不是去要 psutil —— 为了一个数字多装一个依赖不值。
    拿不到就如实说拿不到（macOS、精简容器都会拿不到）。
    """
    info: dict = {"available": False}
    try:
        status = Path("/proc/self/status").read_text()
    except OSError:
        return info
    fields = {}
    for line in status.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    for key, name in (("VmRSS", "rss"), ("VmPeak", "peak")):
        raw = fields.get(key, "")
        if raw.endswith("kB"):
            try:
                info[name] = int(raw.split()[0]) * 1024
            except (ValueError, IndexError):
                pass
    try:
        info["threads"] = int(fields.get("Threads", "0"))
    except ValueError:
        info["threads"] = None
    info["available"] = "rss" in info
    if info.get("rss"):
        info["rss_human"] = human_bytes(info["rss"])
    return info


def log_path() -> Path | None:
    """日志文件在哪：环境变量优先，其次安装脚本写进 .env 的那一行。"""
    raw = os.getenv("SIMPLEURL_LOG", "").strip()
    if not raw:
        env_file = Path(__file__).resolve().parents[2] / ".env"
        try:
            for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("SIMPLEURL_LOG="):
                    raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except OSError:
            return None
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_file() else None


def log_tail(lines: int = 40, window: int = 16_384) -> list[str]:
    """日志尾巴。线上最常被问的一句就是「我这边怎么了」，别让人先 ssh 上去。

    只读末尾 16KB：日志可能是几百 MB，`tail -f` 那套整读一遍会把这个请求拖死。
    """
    path = log_path()
    if path is None:
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - window))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()][-lines:]


def snapshot(db=None) -> dict:
    """一份可读、可展示、可入库的运行快照。"""
    from app.core.admin import is_admin_enabled
    from app.core.clicks import click_buffer
    from app.core.config import settings

    data = {
        "version": __version__,
        "uptime_seconds": uptime(),
        "uptime_human": human_duration(uptime()),
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()}",
        "pid": os.getpid(),
        "memory": memory(),
        "pending_clicks": click_buffer.pending(),
        "dropped_clicks": click_buffer.dropped(),
        "debug": settings.debug,
        "log_path": str(log_path() or ""),
    }
    try:
        data["admin_enabled"] = bool(is_admin_enabled())
    except Exception:  # noqa: BLE001
        data["admin_enabled"] = None
    if db is not None:
        try:
            from app.services import runtime_status

            status = runtime_status(db)
            data.update(
                db_size=status["db_size"],
                links=status["links"],
                click_events=status["click_events"],
                retention_days=status["retention_days"],
            )
        except Exception as exc:  # noqa: BLE001 - 状态页自己不能因为查状态而挂掉
            data["error"] = f"{type(exc).__name__}: {exc}"[:200]
        try:
            from app.services import outbound_blocked

            data["outbound_blocked"] = outbound_blocked()
        except Exception:  # noqa: BLE001
            data["outbound_blocked"] = None
    return data
