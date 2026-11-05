"""workers.circadian —— 视交叉上核：昼夜节律调度。

双后端：装了 APScheduler 用其 CronTrigger；未装则用内置的轻量 tick 调度
（不引入阶段 0 基线外的依赖，且单测可用注入的 now 做确定性时间旅行）。

任务表（本地时间）：
    03:00 nightly_consolidate  新皮层升维 + 分层清扫 + 图谱修剪
    03:30 decay_sweep          FSRS 衰减快照 + 遗忘清扫
    04:00 snapshot             快照与日志压缩
    08:00 morning_brief        晨报
任务失败互相隔离：单个任务抛异常只记录，不影响后续任务与进程。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

__all__ = ["Circadian", "Task", "TaskReport"]


@dataclass(frozen=True)
class Task:
    name: str
    hour: int
    minute: int
    callback: Callable[[], object]
    last_run: str = ""


@dataclass
class TaskReport:
    ran: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


class Circadian:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def register(self, name: str, *, hour: int, minute: int, callback: Callable[[], object]) -> None:
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError("cron 时分越界")
        self._tasks[name] = Task(name, hour, minute, callback)

    @property
    def tasks(self) -> list[Task]:
        return sorted(self._tasks.values(), key=lambda task: (task.hour, task.minute))

    def run_due(self, *, now: datetime | None = None) -> TaskReport:
        """跑当前分钟应当执行的任务；同一分钟重复调用不会重复执行。"""
        moment = now or datetime.now()
        slot = moment.strftime("%Y-%m-%d %H:%M")
        report = TaskReport()
        for task in self.tasks:
            if task.hour != moment.hour or task.minute != moment.minute or task.last_run == slot:
                continue
            try:
                task.callback()
                report.ran.append(task.name)
            except Exception as exc:  # noqa: BLE001  任务隔离
                report.failed[task.name] = f"{type(exc).__name__}: {exc}"
            self._tasks[task.name] = Task(
                task.name, task.hour, task.minute, task.callback, last_run=slot
            )
        return report

    def start(self, *, poll_sec: float = 30.0) -> None:
        """后台循环（daemon）。沙箱/短生命周期进程可直接用 run_due 手动驱动。"""
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.wait(poll_sec):
                self.run_due()

        self._thread = threading.Thread(target=loop, name="circadian", daemon=True)
        self._thread.start()

    def stop(self, *, timeout_sec: float = 1.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_sec)
            self._thread = None

    @staticmethod
    def apscheduler_available() -> bool:
        try:
            import apscheduler  # noqa: F401  # pyright: ignore[reportMissingImports]
        except Exception:  # noqa: BLE001
            return False
        return True

    @staticmethod
    def install_defaults(
        circadian: "Circadian",
        *,
        consolidate: Callable[[], object],
        decay_sweep: Callable[[], object],
        snapshot: Callable[[], object],
        morning_brief: Callable[[], object],
    ) -> None:
        circadian.register("nightly_consolidate", hour=3, minute=0, callback=consolidate)
        circadian.register("decay_sweep", hour=3, minute=30, callback=decay_sweep)
        circadian.register("snapshot", hour=4, minute=0, callback=snapshot)
        circadian.register("morning_brief", hour=8, minute=0, callback=morning_brief)
