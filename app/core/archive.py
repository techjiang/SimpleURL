"""库备份与还原：一条命令与后台下载按钮共用同一套逻辑。

为什么要有这个模块：`simpleurl backup` 原来直接 `tar -czf` 库文件，而站点的库
开着 WAL。SQLite 把「刚写进去的东西」先放在 `-wal` 里，主库文件可能还是几 MB 前的
样子 —— 打包出来的那份能解开、能连上、表也在，就是没有最近的链接。真正的杀手是
它的失败方式：`simpleurl backup` 打印「已备份到 xx」，`simpleurl restore` 打印
「已恢复，服务已重启」，两个人都说成功，丢的数据要等下次用的时候才发现。

所以这里只做一件事：**先把库折成一份自包含的单文件，再交给 tar**。
折叠用 sqlite 自己的 `VACUUM INTO`（副本已合并 WAL、没有半截事务），
目标文件不可用时退回 `Connection.backup()`（逐页拷贝，同样合并 WAL）。
"""

from __future__ import annotations

import io
import os
import sqlite3
import tarfile
import time
from pathlib import Path

DB_NAME = "simpleurl.db"
ENV_NAME = ".env"
# 备份包里除了库和配置，还带一条版本记录 —— 还原时能说清「这是哪个版本的库」，
# 而不是等应用起来在日志里读一句 schema 不对。
INFO_NAME = "BACKUP.txt"


class ArchiveError(Exception):
    """备份 / 还原失败，消息直接给人看。"""


def sqlite_path(database_url: str) -> Path | None:
    """从 DATABASE_URL 解析出库文件；不是文件型 SQLite 就返回 None。"""
    url = (database_url or "").strip()
    if not url.startswith("sqlite"):
        return None
    raw = url.split("sqlite://", 1)[-1].split("?", 1)[0]
    if not raw or raw in {":memory:", "/:memory:"}:
        return None
    # SQLAlchemy 的写法是 `sqlite://` + 一个普通的文件路径：
    #   sqlite:////tmp/a.db  → //tmp/a.db（绝对路径多出来的那一个斜杠）
    #   sqlite:///rel/a.db   → /rel/a.db（相对路径，多出来的那一个斜杠）
    # 不是靠数斜杠猜，而是照着 SQLAlchemy 的规则削掉最前面那一个 ——
    # 以前 `lstrip("/")` 把根目录也吃掉了（/tmp/a.db → tmp/a.db），
    # 于是「库明明在，备份说找不到」。
    if raw.startswith("/"):
        raw = raw[1:]
    return Path(raw).expanduser()


def _copy_via_vacuum(src: Path, dst: Path) -> None:
    conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        conn.execute("VACUUM INTO ?", (str(dst),))
    finally:
        conn.close()


def _copy_via_backup(src: Path, dst: Path) -> None:
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def _table_count(path: Path, table: str = "links") -> int | None:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return int(conn.execute(f"select count(*) from {table}").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def snapshot(db_path: Path, out_dir: Path) -> Path:
    """把库折成一份自包含的单文件，写到 out_dir。返回落点。"""
    if not db_path.is_file():
        raise ArchiveError(f"没有数据库文件：{db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    # 不能用库自己的名字：`VACUUM INTO` 的目标已存在会直接失败，
    # 而安装目录里本来就躺着一个 simpleurl.db —— 折库要折到旁边去。
    out = out_dir / f".{DB_NAME}.snapshot"
    if out.exists():
        out.unlink()
    errors: list[str] = []
    for fn in (_copy_via_vacuum, _copy_via_backup):
        try:
            fn(db_path, out)
            if out.is_file() and out.stat().st_size > 0:
                return out
            errors.append(f"{fn.__name__}: 产出为空")
        except (sqlite3.Error, OSError) as exc:  # pragma: no cover - 取决于环境
            errors.append(f"{fn.__name__}: {exc}")
    raise ArchiveError("库文件折不成可打包的单文件（" + "；".join(errors) + "）")


def _add_file(tar: tarfile.TarFile, path: Path, arcname: str) -> None:
    info = tar.gettarinfo(str(path), arcname=arcname)
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = int(time.time())
    with path.open("rb") as fh:
        tar.addfile(info, fh)


def build_archive(db_path: Path, env_path: Path | None, out_path: Path, *, version: str) -> dict:
    """打包成 tar.gz。返回一份给人看的小结。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = out_path.parent / f".{DB_NAME}.snapshot"
    snap = snapshot(db_path, out_path.parent)
    try:
        lines = [
            f"SimpleURL 备份",
            f"创建时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"应用版本：v{version}",
            f"库文件：{db_path}",
            f"折库方式：VACUUM INTO / Connection.backup（已合并 WAL，无半截事务）",
        ]
        count = _table_count(snap)
        if count is not None:
            lines.append(f"链接条数：{count}")
        info_bytes = ("\n".join(lines) + "\n").encode()
        with tarfile.open(out_path, "w:gz") as tar:
            _add_file(tar, snap, DB_NAME)
            if env_path is not None and env_path.is_file():
                _add_file(tar, env_path, ENV_NAME)
            entry = tarfile.TarInfo(INFO_NAME)
            entry.size = len(info_bytes)
            entry.mode = 0o600
            entry.mtime = int(time.time())
            tar.addfile(entry, io.BytesIO(info_bytes))
        return {
            "path": str(out_path),
            "size": out_path.stat().st_size,
            "links": count,
            "source": str(db_path),
        }
    finally:
        if work.exists():
            work.unlink()
        if snap.exists():
            snap.unlink()


def extract_db(archive_path: Path, out_dir: Path) -> tuple[Path, Path | None, str]:
    """从备份包取出库（必需）与 .env（可选）。解到 out_dir，返回 (db, env, 说明)。"""
    if not archive_path.is_file():
        raise ArchiveError(f"找不到备份文件：{archive_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            names = tar.getnames()
            if DB_NAME not in names:
                raise ArchiveError(f"不是 SimpleURL 的备份包（里面没有 {DB_NAME}）")
            tar.extractall(out_dir, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(out_dir)
    except tarfile.TarError as exc:
        raise ArchiveError(f"不是有效的备份包：{exc}") from exc
    db = out_dir / DB_NAME
    env = out_dir / ENV_NAME
    note = ""
    info = out_dir / INFO_NAME
    if info.is_file():
        try:
            note = info.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            note = ""
    return db, (env if env.is_file() else None), note


def verify(db: Path) -> int:
    """确认这份库真的能读：完整性检查通过、links 表在。返回链接条数。"""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            verdict = conn.execute("PRAGMA integrity_check").fetchone()
            if not verdict or verdict[0] != "ok":
                raise ArchiveError(f"备份包里的库不完整：{verdict[0] if verdict else '未知'}")
            return int(conn.execute("select count(*) from links").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise ArchiveError(f"备份包里的库读不出来：{exc}") from exc


def env_file_for(database_url: str) -> Path | None:
    """安装目录里的 .env：库文件所在目录的那一份。"""
    db = sqlite_path(database_url)
    if db is None:
        return None
    candidate = db.parent / ENV_NAME
    return candidate if candidate.is_file() else None


def default_backup_path(home: Path | None = None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = home or Path.home()
    return base / f"simpleurl-backup-{stamp}.tar.gz"


__all__ = [
    "ArchiveError",
    "build_archive",
    "default_backup_path",
    "env_file_for",
    "extract_db",
    "sqlite_path",
    "verify",
]
