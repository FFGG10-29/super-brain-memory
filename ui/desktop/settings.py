"""ui.desktop.settings —— PAT 与 SYNC_REPO_URL 的安全配置存储。

三级策略（架构风险清单：PAT 泄露 → 钥匙串 + 最小权限 + 短周期轮换）：
    SYNC_REPO_URL → QSettings（用户级，不进仓库）
    GITHUB_PAT    → 系统钥匙串（Windows Credential Manager / macOS Keychain / libsecret）
                    回退顺序：钥匙串 > 环境变量 GITHUB_PAT > 未配置
    两者都**不写项目 .env、不写事件日志、不 print 明文**。

测试用 `keyring_backend` / `qsettings` 注入内存替身，不碰用户真实凭据。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QSettings

from config import BrainConfig, get_config
from ui.desktop.secret_store import SecretStore

__all__ = ["SettingsStore", "SyncSettings"]

_SERVICE = "super-brain-v21"
_PAT_ACCOUNT = "github_pat"
_REPO_KEY = "sync/repo_url"


@dataclass(frozen=True)
class SyncSettings:
    repo_url: str
    pat_present: bool
    pat_source: str                  # keyring | env | none
    device_id: str

    @property
    def masked_pat(self) -> str:
        return "••••••••" if self.pat_present else "未配置"

    def as_status(self) -> dict[str, str]:
        return {
            "repo_url": self.repo_url or "未配置",
            "pat": self.masked_pat,
            "pat_source": self.pat_source,
            "device_id": self.device_id,
        }


class SettingsStore:
    def __init__(
        self,
        config: BrainConfig | None = None,
        *,
        keyring_backend: Any | None = None,
        qsettings: Any | None = None,
    ) -> None:
        self._config = config or get_config()
        self._keyring = keyring_backend if keyring_backend is not None else default_keyring()
        self._q = qsettings if qsettings is not None else QSettings("super-brain", "desktop")
        # 钥匙串不可用时落 DPAPI 加密文件，保证「填一次就一直是它」
        self._secrets = SecretStore(keyring_backend=self._keyring, service=_SERVICE)

    def _read_pat(self) -> tuple[str | None, str]:
        value = self._secrets.get(_PAT_ACCOUNT)
        if not value:
            return None, "none"
        backend = self._secrets.backend_of(_PAT_ACCOUNT)
        if backend != "none":
            return value, backend
        return value, "env"

    def load(self) -> SyncSettings:
        repo_url = str(self._q.value(_REPO_KEY, "") or "") or self._config.sync.repo_url
        _, source = self._read_pat()
        return SyncSettings(repo_url=repo_url, pat_present=source != "none", pat_source=source,
                            device_id=self._config.sync.device_id)

    def pat(self) -> str | None:
        return self._read_pat()[0]

    def save(self, repo_url: str, pat: str | None) -> SyncSettings:
        """保存配置。pat=None 表示"不改动现有凭据"；空字符串表示清除。"""
        repo_url = repo_url.strip()
        self._q.setValue(_REPO_KEY, repo_url)
        self._q.sync()
        self._config.sync.repo_url = repo_url

        if pat is not None:
            backend = self._secrets.set(_PAT_ACCOUNT, pat)
            if pat.strip() and backend == "none":
                raise RuntimeError(
                    "PAT 保存失败：系统钥匙串与加密文件都不可写。"
                    "可临时把 GITHUB_PAT 放进环境变量，或检查 %APPDATA%/super-brain 目录权限"
                )
        return self.load()

    # ── 界面/网关偏好（同样只进用户级 QSettings）────────────────────────
    def load_prefs(self) -> dict[str, Any]:
        return {
            "http_host": str(self._q.value("gateway/host", self._config.gateway.http_host) or "127.0.0.1"),
            "http_port": _as_int(self._q.value("gateway/port", self._config.gateway.http_port), 8766),
            "model_name": str(self._q.value("gateway/model", "super-brain-memory")),
            "timer_interval_ms": _as_int(self._q.value("ui/timer_ms", self._config.ui.timer_interval_ms), 16),
            "similarity_threshold": _as_float(
                self._q.value("ui/threshold", self._config.activation.similarity_threshold), 0.32
            ),
            "max_labels": _as_int(self._q.value("ui/max_labels", self._config.ui.max_labels), 14),
        }

    def save_prefs(self, **prefs: Any) -> dict[str, Any]:
        mapping = {
            "http_host": "gateway/host",
            "http_port": "gateway/port",
            "model_name": "gateway/model",
            "timer_interval_ms": "ui/timer_ms",
            "similarity_threshold": "ui/threshold",
            "max_labels": "ui/max_labels",
        }
        for key, value in prefs.items():
            if key not in mapping:
                raise KeyError(f"未知偏好项：{key}")
            self._q.setValue(mapping[key], value)
        self._q.sync()
        if "http_host" in prefs:
            self._config.gateway.http_host = str(prefs["http_host"])
        if "http_port" in prefs:
            self._config.gateway.http_port = int(prefs["http_port"])
        if "timer_interval_ms" in prefs:
            self._config.ui.timer_interval_ms = int(prefs["timer_interval_ms"])
        if "similarity_threshold" in prefs:
            self._config.activation.similarity_threshold = float(prefs["similarity_threshold"])
        if "max_labels" in prefs:
            self._config.ui.max_labels = int(prefs["max_labels"])
        return self.load_prefs()

    def test_connection(self) -> dict[str, Any]:
        """只做本地校验，不发起带凭据的网络请求（避免 PAT 进入命令行/进程列表）。"""
        settings = self.load()
        url = settings.repo_url
        url_ok = url.startswith(("https://", "git@", "ssh://"))
        # 只探测可执行文件，不 spawn 子进程：Qt/沙箱环境下句柄继承可能受限，
        # 且带凭据的 git 命令绝不能出现在这里（PAT 不得进入进程列表）
        git_available = shutil.which("git") is not None
        version = "git available" if git_available else ""
        return {
            "url_ok": url_ok,
            "pat_present": settings.pat_present,
            "git_available": git_available,
            "git_version": version,
            "message": (
                "配置就绪：真实 pull/push 由 git_worker 执行（PAT 从钥匙串/环境变量注入）"
                if url_ok and git_available and settings.pat_present
                else "请补全仓库地址 / PAT / git 可执行文件"
            ),
        }


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def default_keyring():
    try:
        import keyring  # pyright: ignore[reportMissingImports]
    except Exception:  # noqa: BLE001
        return None
    return keyring
