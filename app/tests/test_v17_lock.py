"""v17：创建链路上的数据库写锁。

单独一个文件，是因为这些用例依赖 `create_link` / `apply_title` 的拆分。
放在一起的话，「撤掉其中一处修复」会让整个文件 import 失败，
其余用例跟着全红——那就看不出到底哪条测试在钉哪个行为了。
"""

from __future__ import annotations

import threading
import time

class TestTitleFetchOutsideTransaction:
    """抓标题不能拿着数据库写锁等网络。

    以前 `create_link` 里 flush 之后立刻抓标题，抓一次最长等 5 秒，
    这段时间事务一直开着。SQLite 的写锁是全局的，于是「一个慢域名」
    等于「全站写请求排队」，排队超过连接超时（15 秒）直接
    `database is locked`。用例用时间断言，不靠读代码确认。
    """

    def test_create_does_not_hold_write_lock_during_fetch(self, client, monkeypatch):
        from app.core import titles

        def slow(url, timeout=5):
            time.sleep(1.0)
            return "慢标题"

        monkeypatch.setattr(titles, "fetch_title_sync", slow)

        done: list[float] = []

        def writer():
            started = time.monotonic()
            # 另一个写请求：改一条设置，走的也是写事务
            client.post("/api/admin/settings", json={"site_tagline": f"并发写入 {len(done)}"})
            done.append(time.monotonic() - started)

        th = threading.Thread(target=writer)
        th.start()
        time.sleep(0.15)
        r = client.post("/api/links", json={"target": "https://example.com/slow-title"})
        th.join(timeout=20)

        assert r.status_code == 201
        assert r.json()["title"] == "慢标题"
        assert done, "并发写请求没有完成"
        # 老写法下这个写请求要等到抓标题结束（1 秒）才拿到锁
        assert done[0] < 0.9, f"写请求被标题抓取堵住了 {done[0]:.2f}s"

    def test_title_still_saved_after_commit(self, client, monkeypatch):
        from app.core import titles

        monkeypatch.setattr(titles, "fetch_title_sync", lambda url, timeout=5: "抓回来的标题")
        r = client.post("/api/links", json={"target": "https://example.com/a/b"})
        code = r.json()["code"]
        # 标题是在提交之后补上的，必须真的落库，不能只在响应里出现过
        assert client.get(f"/api/links/{code}").json()["title"] == "抓回来的标题"
        assert client.get(f"/api/links/{code}").json()["title_source"] == "fetched"

    def test_manual_title_untouched(self, client, monkeypatch):
        from app.core import titles

        called: list[str] = []
        monkeypatch.setattr(titles, "fetch_title_sync", lambda url, timeout=5: called.append(url) or "不该出现")
        r = client.post("/api/links", json={"target": "https://example.com/m", "title": "我自己填的"})
        assert r.json()["title"] == "我自己填的"
        assert r.json()["title_source"] == "manual"
        assert called == []
