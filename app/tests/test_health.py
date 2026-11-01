"""系统信息与日志尾巴：这几个数字是排障时的第一手材料，值得钉住。"""

from __future__ import annotations


class TestSystemApi:
    def test_requires_admin(self, client):
        r = client.get("/api/admin/system")
        assert r.status_code == 200
        assert r.json()["version"]

    def test_fields(self, client):
        data = client.get("/api/admin/system").json()
        for key in ("version", "uptime_human", "pid", "python", "platform", "memory", "pending_clicks"):
            assert key in data
        assert data["uptime_seconds"] >= 0

    def test_log_hidden_by_default(self, client):
        # 日志可能是几百 KB，默认不传，免得每次刷面板都白等
        assert client.get("/api/admin/system").json()["log_tail"] == []

    def test_counts_links(self, client):
        client.post("/api/links", json={"target": "https://example.com/health-check"})
        data = client.get("/api/admin/system").json()
        assert data["links"] == 1
        assert data["db_size"] is not None


class TestLogTail:
    def test_reads_last_lines(self, tmp_path, monkeypatch):
        from app.core import health

        log = tmp_path / "simpleurl.log"
        log.write_text("\n".join(f"line {i}" for i in range(200)), encoding="utf-8")
        monkeypatch.setenv("SIMPLEURL_LOG", str(log))
        tail = health.log_tail(lines=5)
        assert tail == ["line 195", "line 196", "line 197", "line 198", "line 199"]

    def test_missing_file_is_empty(self, tmp_path, monkeypatch):
        from app.core import health

        monkeypatch.setenv("SIMPLEURL_LOG", str(tmp_path / "nope.log"))
        assert health.log_tail() == []
        assert health.log_path() is None

    def test_no_config_is_empty(self, monkeypatch):
        from app.core import health

        monkeypatch.delenv("SIMPLEURL_LOG", raising=False)
        monkeypatch.setattr(health, "log_path", lambda: None)
        assert health.log_tail() == []


class TestHumanize:
    def test_duration(self):
        from app.core.health import human_duration

        assert human_duration(0) == "0秒"
        assert human_duration(65) == "1分 5秒"
        assert human_duration(3700) == "1小时 1分"
        assert human_duration(90000) == "1天 1小时"

    def test_bytes(self):
        from app.core.health import human_bytes

        assert human_bytes(None) == "—"
        assert human_bytes(512) == "512 B"
        assert human_bytes(2048) == "2.0 KB"

    def test_memory(self):
        from app.core.health import memory

        info = memory()
        # Linux 上读得到，其它平台允许拿不到，但结构必须稳定
        assert "available" in info
        if info["available"]:
            assert info["rss"] > 0
