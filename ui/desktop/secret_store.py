"""ui.desktop.secret_store —— 密钥持久化：系统钥匙串优先，DPAPI 加密文件兜底。

为什么需要兜底：打包后的机器上 `keyring` 后端可能不可用（缺 pywin32-ctypes、
凭据管理器被策略禁用、便携环境没有用户配置），此时旧实现直接抛错或静默丢弃，
表现就是「这次填了 PAT，下次打开又要重填」。

Windows 上用 DPAPI（CryptProtectData）加密后写 `%APPDATA%/super-brain/secrets.bin`：
- 密文只有**当前 Windows 用户**能解，换用户/换机无法读取；
- 不落明文，不进仓库、不进事件日志；
- 仍优先使用系统钥匙串（更规范），只有钥匙串不可用时才落文件。
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

from core.runtime_paths import user_dir

__all__ = ["SecretStore", "default_secret_store"]


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


if sys.platform == "win32":
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # 必须显式声明 argtypes/restype：64 位下默认 c_int 会把指针截断成 32 位，
    # LocalFree 会破坏堆，表现为进程退出时 0xC0000409。
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p


def _blob(data: bytes) -> tuple[ctypes.Array, _DataBlob]:
    """构造 DATA_BLOB；返回 (buffer, blob)，buffer 必须由调用方持有防 GC。"""
    buffer = ctypes.create_string_buffer(data, len(data))
    return buffer, _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _dpapi_encrypt(data: bytes) -> bytes:
    buffer, blob_in = _blob(data)
    blob_out = _DataBlob()
    if not _crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError(f"DPAPI 加密失败（{ctypes.get_last_error()}）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def _dpapi_decrypt(data: bytes) -> bytes:
    buffer, blob_in = _blob(data)
    blob_out = _DataBlob()
    if not _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError(f"DPAPI 解密失败（{ctypes.get_last_error()}）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


class SecretStore:
    """set/get/delete 三个动作，钥匙串失败自动走加密文件。"""

    def __init__(self, *, keyring_backend: Any | None = None, service: str = "super-brain-v21") -> None:
        self._keyring = keyring_backend
        self._service = service
        self._path = user_dir() / "secrets.bin"

    # ── 加密文件兜底 ──────────────────────────────────────────────────────
    def _read_file(self) -> dict[str, str]:
        if not self._path.is_file():
            return {}
        try:
            raw = _dpapi_decrypt(self._path.read_bytes()) if sys.platform == "win32" else self._path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
            return {str(k): str(v) for k, v in payload.items()} if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_file(self, values: dict[str, str]) -> bool:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            raw = json.dumps(values, ensure_ascii=False).encode("utf-8")
            data = _dpapi_encrypt(raw) if sys.platform == "win32" else raw
            self._path.write_bytes(data)
            return True
        except OSError:
            return False

    # ── 对外接口 ──────────────────────────────────────────────────────────
    def get(self, account: str) -> str | None:
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(self._service, account)
                if value:
                    return value
            except Exception:  # noqa: BLE001  钥匙串不可用不致命
                pass
        return self._read_file().get(account) or os.environ.get("GITHUB_PAT") or None

    def set(self, account: str, value: str) -> str:
        """保存密钥，返回实际使用的后端（keyring / file / none）。"""
        value = value.strip()
        if not value:
            self.delete(account)
            return "none"
        if self._keyring is not None:
            try:
                self._keyring.set_password(self._service, account, value)
                return "keyring"
            except Exception:  # noqa: BLE001
                pass
        values = self._read_file()
        values[account] = value
        return "file" if self._write_file(values) else "none"

    def delete(self, account: str) -> None:
        if self._keyring is not None:
            try:
                self._keyring.delete_password(self._service, account)
            except Exception:  # noqa: BLE001
                pass
        values = self._read_file()
        if values.pop(account, None) is not None:
            self._write_file(values)

    def backend_of(self, account: str) -> str:
        if self._keyring is not None:
            try:
                if self._keyring.get_password(self._service, account):
                    return "keyring"
            except Exception:  # noqa: BLE001
                pass
        return "file" if self._read_file().get(account) else "none"


_STORE: SecretStore | None = None


def default_secret_store() -> SecretStore:
    global _STORE
    if _STORE is None:
        from ui.desktop.settings import default_keyring

        _STORE = SecretStore(keyring_backend=default_keyring())
    return _STORE

