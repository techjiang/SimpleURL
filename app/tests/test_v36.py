"""v1.6.2 / install.sh v2.8：备份到底有没有把最近的数据带上。

这一轮的四条都是「两条命令各自说成功、数据却在半路丢了」那种：
· `simpleurl backup` 只打主库，最近的写入还在 -wal 里 —— 包能解开、库能开、就是没有新数据
· 库路径解析把 `sqlite:////tmp/x.db` 的根目录削掉了
· 所有 POST 的表单都被 YOURLS 兼容层的中间件读过一次，文件上传永远拿不到文件
· 从安装目录之外拷一份新脚本去装，包里那份旧脚本又把新脚本顶了回去
"""

from __future__ import annotations

import io
import sqlite3
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "install.sh"
BODY = SCRIPT.read_text(encoding="utf-8")


# ---------------------------------------------------------------- archive


def _make_db(path: Path, codes: list[str], *, wal: bool = True) -> None:
    conn = sqlite3.connect(str(path))
    try:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE links (code TEXT PRIMARY KEY, target TEXT)")
        for code in codes:
            conn.execute("INSERT INTO links VALUES (?, ?)", (code, f"https://example.com/{code}"))
        conn.commit()
    finally:
        conn.close()


def _codes(db: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute("select code from links order by code")]
    finally:
        conn.close()


class TestSqlitePath:
    """`sqlite:////tmp/x.db` 的四个斜杠各有含义。"""

    def test_absolute_url_keeps_root_slash(self, tmp_path):
        from app.core.archive import sqlite_path

        assert sqlite_path(f"sqlite:///{tmp_path}/a.db") == Path(f"{tmp_path}/a.db")

    def test_relative_url_keeps_relative(self):
        from app.core.archive import sqlite_path

        assert sqlite_path("sqlite:///rel/a.db") == Path("rel/a.db")

    def test_memory_and_postgres_are_not_files(self):
        from app.core.archive import sqlite_path

        assert sqlite_path("sqlite:///:memory:") is None
        assert sqlite_path("sqlite://") is None
        assert sqlite_path("postgresql://u:p@h/db") is None

    def test_double_slash_does_not_break_sqlite_uri(self, tmp_path):
        """//tmp/x.db 在 SQLite 的 URI 里是 authority，会直接报错。"""
        from app.core.archive import sqlite_path

        db = tmp_path / "a.db"
        _make_db(db, ["k"], wal=False)
        got = sqlite_path(f"sqlite:///{db}")
        assert str(got) == str(db)
        conn = sqlite3.connect(f"file:{got}?mode=ro", uri=True)
        try:
            assert conn.execute("select count(*) from links").fetchone()[0] == 1
        finally:
            conn.close()


class TestSnapshotMergesWal:
    """备份包里必须有最近的写入，否则「备份成功」是句空话。"""

    def test_recent_writes_show_up_in_snapshot(self, tmp_path):
        from app.core.archive import snapshot

        src = tmp_path / "live.db"
        _make_db(src, ["aaa"])
        # 再写一条，并保持连接不关：数据先落 -wal，主库还是老样子
        keep = sqlite3.connect(str(src))
        keep.execute("PRAGMA journal_mode=WAL")
        keep.execute("INSERT INTO links VALUES ('bbb', 'https://example.com/b')")
        keep.commit()
        try:
            assert _codes(src) == ["aaa", "bbb"]  # 主库 + WAL 才是全貌
            out = snapshot(src, tmp_path / "snap")
            assert _codes(out) == ["aaa", "bbb"], "折出来的副本必须合并 WAL"
        finally:
            keep.close()

    def test_snapshot_refuses_missing_file(self, tmp_path):
        from app.core.archive import ArchiveError, snapshot

        with pytest.raises(ArchiveError):
            snapshot(tmp_path / "nope.db", tmp_path)


class TestArchiveRoundTrip:
    def test_build_then_extract_keeps_data(self, tmp_path):
        from app.core.archive import build_archive, extract_db, verify

        db = tmp_path / "simpleurl.db"
        _make_db(db, ["x1", "x2"])
        env = tmp_path / ".env"
        env.write_text("ADMIN_USERNAME=admin\n", encoding="utf-8")
        out = tmp_path / "b.tar.gz"
        info = build_archive(db, env, out, version="9.9.9")
        assert info["links"] == 2

        with tarfile.open(out) as tar:
            names = tar.getnames()
        assert "simpleurl.db" in names and ".env" in names and "BACKUP.txt" in names
        # 打包进去的必须是折好的单文件，不是「主库 + 指望旁边有 -wal」
        assert not any(n.endswith("-wal") for n in names)

        got, got_env, note = extract_db(out, tmp_path / "x")
        assert got_env is not None
        assert "9.9.9" in note
        assert _codes(got) == ["x1", "x2"]

    def test_verify_rejects_broken_db(self, tmp_path):
        from app.core.archive import ArchiveError, verify

        bad = tmp_path / "bad.db"
        bad.write_bytes(b"not a database at all")
        with pytest.raises(ArchiveError):
            verify(bad)

    def test_extract_rejects_foreign_tarball(self, tmp_path):
        from app.core.archive import ArchiveError, extract_db

        other = tmp_path / "other.tar.gz"
        with tarfile.open(other, "w:gz") as tar:
            info = tarfile.TarInfo("readme.txt")
            payload = b"hello"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        with pytest.raises(ArchiveError):
            extract_db(other, tmp_path / "x")


# ---------------------------------------------------------------- 上传路径


class TestUploadReachesRoute:
    """YOURLS 兼容层的表单缓存中间件以前对所有 POST 都读一次 request.form()。"""

    def test_middleware_only_touches_yourls_paths(self):
        src = (ROOT / "app" / "yourls_api.py").read_text(encoding="utf-8")
        block = src[src.index("class _FormCacheMiddleware") : src.index("def _check_signature")]
        assert 'startswith("/yourls-api")' in block, "只该读自己的路径"
        assert 'request.method == "POST" and request.url.path.startswith' in block

    def test_backup_upload_endpoint_accepts_file(self):
        src = (ROOT / "app" / "admin_api.py").read_text(encoding="utf-8")
        assert "async def api_restore" in src
        assert "UploadFile" in src


# ---------------------------------------------------------------- 安装脚本


def _extract_fn(name: str) -> str:
    lines = BODY.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    depth = 0
    for i in range(start, len(lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if depth <= 0:
            return "\n".join(lines[start : i + 1])
    raise AssertionError(f"{name} 没配平")


class TestInstallScriptFixes:
    def test_backup_uses_archive_module(self):
        fn = _extract_fn("do_backup")
        assert "app/core/archive.py" in fn or "build_archive" in fn
        # 兜底那条路也必须带上 -wal / -shm
        assert "simpleurl.db-wal" in fn
        assert "simpleurl.db-shm" in fn

    def test_restore_keeps_a_copy_before_overwrite(self):
        fn = _extract_fn("do_restore")
        assert "restore-backup" in fn
        assert "verify(" in fn

    def test_doctor_resolves_installed_dir(self):
        """任意路径下体检，得认出这台机器上那份安装。"""
        assert "_installed_dir()" in BODY
        assert 'doctor|check)' in BODY
        block = BODY[BODY.index("doctor|check)") :][:600]
        assert "_installed_dir" in block

    def test_env_template_has_no_comment_mentioning_token(self):
        """`grep ADMIN_TOKEN .env` 只该匹配到真正启用的那一行。"""
        env_fn = _extract_fn("write_env")
        for line in env_fn.splitlines():
            if "ADMIN_TOKEN" in line:
                assert not line.lstrip().startswith("#"), f"注释里别再提 ADMIN_TOKEN：{line}"

    def test_self_script_wins_when_packaged_is_older(self):
        assert "_prefer_self_script()" in BODY
        fn = _extract_fn("_prefer_self_script")
        assert "version_lt" in fn


class TestRestoreEndpointButNotOnEveryPost:
    """CSRF 与 multipart 不能互相打架。"""

    def test_csrf_still_required_for_upload(self):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        r = client.post(
            "/api/admin/restore",
            files={"file": ("a.tar.gz", b"\x1f\x8b", "application/gzip")},
        )
        assert r.status_code in (401, 403), "没登录就不该进得去"
