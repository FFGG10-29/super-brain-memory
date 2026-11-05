"""ui.desktop.async_backend —— 把重后端调用挪出 Qt 主线程。

单线程队列（maxThreadCount=1）保证同一时刻只有一个任务碰六引擎，结果经
QueuedConnection 回到主线程再更新控件：模型推理期间 UI 不冻结、星云动画继续跑。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from PySide6 import QtCore

__all__ = ["AsyncBackend"]


class _Signals(QtCore.QObject):
    done = QtCore.Signal(object)
    failed = QtCore.Signal(str)


class _Task(QtCore.QRunnable):
    def __init__(self, fn: Callable[[], Any], signals: _Signals, lock: threading.RLock) -> None:
        super().__init__()
        self._fn = fn
        self._signals = signals
        self._lock = lock

    def run(self) -> None:
        try:
            with self._lock:
                self._signals.done.emit(self._fn())
        except Exception as exc:  # noqa: BLE001  后台异常回主线程显示，不炸进程
            self._signals.failed.emit(f"{type(exc).__name__}: {exc}")


class AsyncBackend(QtCore.QObject):
    """串行任务队列；lock 供 UI 轮询判断"后台忙"以便跳过本轮刷新。"""

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QtCore.QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._pending: set[_Signals] = set()
        self.lock = threading.RLock()

    @property
    def busy(self) -> bool:
        return self._pool.activeThreadCount() > 0

    def submit(
        self,
        fn: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        signals = _Signals()
        self._pending.add(signals)

        def _done(value: Any) -> None:
            self._pending.discard(signals)
            on_done(value)

        def _failed(message: str) -> None:
            self._pending.discard(signals)
            if on_error is not None:
                on_error(message)

        queued = QtCore.Qt.ConnectionType.QueuedConnection
        signals.done.connect(_done, queued)
        signals.failed.connect(_failed, queued)
        self._pool.start(_Task(fn, signals, self.lock))

    def flush(self, msecs: int = 30000) -> None:
        """等待队列完成并把结果回调派发到主线程（测试 / 离屏截图用）。"""
        self.wait(msecs)
        app = QtCore.QCoreApplication.instance()
        if app is not None:
            app.processEvents()
            app.processEvents()

    def wait(self, msecs: int = 30000) -> None:
        """窗口关闭前调用，避免后台任务触碰已销毁的对象。"""
        self._pool.waitForDone(msecs)
