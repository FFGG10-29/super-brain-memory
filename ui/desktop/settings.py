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
        self._keyring = keyring_backend if keyring_backend is not None else _default_keyring()
        self._q = qsettings if qsettings is not None else QSettings("super-brain", "desktop")

    def _read_pat(self) -> tuple[str | None, str]:
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(_SERVICE, _PAT_ACCOUNT)
                if value:
                    return value, "keyring"
            except Exception:  # noqa: BLE001  钥匙串不可用时静默回退到 env
                pass
        env_value = os.environ.get("GITHUB_PAT") or ""
        return (env_value, "env") if env_value else (None, "none")

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
            if self._keyring is None:
                raise RuntimeError(
                    "系统钥匙串不可用：请把 PAT 放到环境变量 GITHUB_PAT，不要写进项目文件"
                )
            if pat.strip():
                self._keyring.set_password(_SERVICE, _PAT_ACCOUNT, pat.strip())
            else:
                try:
                    self._keyring.delete_password(_SERVICE, _PAT_ACCOUNT)
                except Exception:  # noqa: BLE001
                    pass
        return self.load()

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


def _default_keyring():
    try:
        import keyring  # pyright: ignore[reportMissingImports]
    except Exception:  # noqa: BLE001
        return None
    return keyring
