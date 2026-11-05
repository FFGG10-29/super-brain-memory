"""sync.git_worker —— pull → 重放 → push 循环（定时 + 写入防抖）。

断网纪律：本地写入始终先 fsync 到 journal（不经过本模块），git 失败只记日志
并退避重试，**绝不阻塞认知层、绝不丢事件**。
结构性免冲突：每设备只写 journal/devices/{device_id}/ 自己目录。

重放策略：每次全量扫描 + event_id 去重表（relational.mark_event_applied），
因此游标丢失/重复执行都安全 —— 对应阶段 5「重放幂等」验收。
"""
from __future__ import annotations

import base64
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.errors import SyncError
from core.ids import HLCStamp
from core.interfaces import Projector
from sync.event_store import EventStore

__all__ = ["GitSyncWorker", "SyncReport"]

_DEVICES_DIR = "devices"


@dataclass
class SyncReport:
    pulled: bool = False
    replayed: int = 0
    skipped: int = 0
    pushed: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class GitSyncWorker:
    def __init__(
        self,
        *,
        root: str | Path,
        device_id: str,
        event_store: EventStore,
        projector: Projector,
        remote: str = "origin",
        branch: str = "main",
        repo_url: str = "",
        token: str | None = None,
        sync_interval_sec: int = 300,
        write_debounce_sec: float = 2.0,
    ) -> None:
        self._root = Path(root)
        self._device_id = device_id
        self._store = event_store
        self._projector = projector
        self._remote = remote
        self._branch = branch
        self._repo_url = str(repo_url or "").strip()
        self._token = token
        self._interval = float(sync_interval_sec)
        self._debounce = float(write_debounce_sec)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # ── Git 操作（无仓库/无网络时降级为 no-op）───────────────────────────
    def _auth_env(self) -> dict[str, str]:
        """把 PAT 通过环境变量注入 git，而不是命令行参数。

        `-c http.extraheader=...` 会出现在进程命令行里（任务管理器可见），
        违反"PAT 不进入命令行"的纪律；GIT_CONFIG_COUNT 系列只存在于子进程环境。
        """
        env = dict(os.environ)
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        if self._token and self._repo_url.startswith("https://"):
            credential = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = f"http.{self._repo_url.rstrip('/')}/.extraheader"
            env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {credential}"
        return env

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self._root), *args],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
            env=self._auth_env(),
        )
        if result.returncode != 0:
            raise SyncError(f"git {' '.join(args)} 失败: {result.stderr.strip() or result.stdout.strip()}")
        return result.stdout

    @property
    def has_repo(self) -> bool:
        return (self._root / ".git").exists()

    def _git_query(self, *args: str) -> str:
        """查询类命令（如 git config 读值）：未设置时返回空串而不是抛错。"""
        result = subprocess.run(
            ["git", "-C", str(self._root), *args],
            capture_output=True, text=True, timeout=30, check=False, env=self._auth_env(),
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def ensure_repo(self) -> bool:
        """首次同步时把事件目录初始化成 git 仓库并接上远端。

        没有这一步，只有 `.git` 已存在才会 push —— 用户在界面上填了仓库地址、
        点「立即同步」却永远 SKIP，事件一个都传不上去（历史版本就是这个行为）。
        """
        if self.has_repo:
            return True
        if not self._repo_url:
            return False
        self._root.mkdir(parents=True, exist_ok=True)
        self._git("init")
        # 提交身份：只在没配过的机器上兜底，避免 "Author identity unknown" 直接卡死同步
        if not self._git_query("config", "user.name"):
            self._git("config", "user.name", "super-brain")
        if not self._git_query("config", "user.email"):
            self._git("config", "user.email", "super-brain@localhost")
        self._git("remote", "add", self._remote, self._repo_url)
        self._git("symbolic-ref", "HEAD", f"refs/heads/{self._branch}")
        try:
            self._git("fetch", self._remote, self._branch)
            # 远端已有历史：直接把本地空仓库对齐过去，避免 unrelated histories 冲突
            self._git("reset", "--hard", f"{self._remote}/{self._branch}")
        except SyncError:
            pass  # 空仓库：首次 push 会自动建分支
        return True

    def pull(self, report: SyncReport) -> None:
        if not self.ensure_repo():
            return
        try:
            self._git("pull", "--rebase", self._remote, self._branch)
            report.pulled = True
        except SyncError as exc:
            # 首次推送前远端还是空的：pull 失败不算错误，push 会建分支
            if "couldn't find remote ref" not in str(exc) and "no such remote" not in str(exc):
                raise

    def push(self, report: SyncReport) -> None:
        if not self.ensure_repo():
            return
        self._git("add", "-A")
        if not self._git("status", "--porcelain").strip():
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self._git("commit", "-m", f"sync({self._device_id}) {stamp}")
        self._git("push", "-u", self._remote, self._branch)
        report.pushed = True

    # ── 重放 ──────────────────────────────────────────────────────────────
    def replay(self, report: SyncReport) -> None:
        events = self._store.scan(include_self=False, device_id=self._device_id)
        maxima: dict[str, HLCStamp] = {}
        for event in events:
            device = str(event.get("device_id", "unknown"))
            stamp = HLCStamp.from_token(str(event["hlc"]))
            self._projector.apply(event)
            if getattr(self._projector, "last_applied", None) is True:
                report.replayed += 1
            else:
                report.skipped += 1
            if device not in maxima or stamp > maxima[device]:
                maxima[device] = stamp
        for device, stamp in maxima.items():
            self._store.save_cursor(device, stamp)

    def sync_once(self) -> SyncReport:
        report = SyncReport()
        try:
            self.pull(report)
        except (SyncError, OSError, subprocess.SubprocessError) as exc:
            report.errors.append(f"pull: {exc}")
        try:
            self.replay(report)
        except (SyncError, OSError, ValueError) as exc:
            report.errors.append(f"replay: {exc}")
        try:
            self.push(report)
        except (SyncError, OSError, subprocess.SubprocessError) as exc:
            report.errors.append(f"push: {exc}")
        return report

    # ── 定时 + 写入防抖 ──────────────────────────────────────────────────
    def notify_write(self) -> None:
        """写入方在 fsync 后调用：唤醒同步线程（防抖窗口内合并多次写入）。"""
        self._wake.set()

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.is_set():
                triggered = self._wake.wait(self._interval)
                if self._stop.is_set():
                    return
                if triggered:
                    self._wake.clear()
                    if self._stop.wait(min(self._debounce, self._interval)):
                        return
                self.sync_once()

        self._thread = threading.Thread(target=loop, name="git-sync", daemon=True)
        self._thread.start()

    def stop(self, *, timeout_sec: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_sec)
            self._thread = None
