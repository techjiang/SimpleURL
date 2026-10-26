"""后台维护：按保留期清理点击明细。

单独起一个线程，而不是塞进请求里顺手做——清理是「迟早要做、但不急」的事，
让访客的请求去等一次批量删除是拿体验换空间。启动时先跑一次，之后每小时一次。
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("simpleurl")

INTERVAL = 3600


class Maintenance:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="maintenance", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        self.run_once()
        while not self._stop.wait(INTERVAL):
            self.run_once()

    def run_once(self) -> int:
        try:
            from app.services import purge_old_clicks

            return purge_old_clicks()
        except Exception as exc:  # 清理失败不能影响服务本身
            logger.warning("点击明细清理失败：%s", exc)
            return 0

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)


maintenance = Maintenance()
