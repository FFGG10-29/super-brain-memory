"""core.desktop_features —— 开机自启 / 密钥保管 / 数据目录迁移（与 UI 框架无关）。

系统级桌面能力（开机自启 / 密钥保管 / 数据目录迁移）统一在 core.* 实现，
与任何 UI 框架无关，后端冻结包无 GUI。
- 自启：Windows 用户级 Run 键（不写系统目录，不需要管理员）。
- 密钥：优先系统 keyring，不可用则用 DPAPI 加密文件兜底（只有当前 Windows 用户能解）。
- 数据目录迁移：复制到目标 + 写 `data_pointer.json`，后端重启后由 `runtime_paths.data_dir()` 生效。
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from core.runtime_paths import data_dir, user_dir

__all__ = [
    "autostart_command", "autostart_enabled", "set_autostart",
    "SecretStore", "default_secret_store",
    "data_status", "migrate_data", "clear_data_pointer",
]

# ── 开机自启 ────────────────────────────────────────────────────────────
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "SuperBrainFlutter"
_TRAY_FLAG = "--tray"


def autostart_command(target: str = "") -> str:
    """Flutter 前端 exe 路径由调用方传入；缺省退回当前 Python 解释器（开发态）。"""
    exe = target.strip() or sys.executable
    return f'"{exe}" {_TRAY_FLAG}'


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _VALUE_NAME)
            return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool, target: str = "") -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, autostart_command(target))
            else:
                try:
                    winreg.DeleteValue(key, _VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


# ── 密钥保管（DPAPI 兜底）──────────────────────────────────────────────
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


if sys.platform == "win32":
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # 64 位必须显式声明 argtypes/restype，否则指针被截断成 32 位，LocalFree 会破坏堆。
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


def _blob(data: bytes) -> tuple[Any, _DataBlob]:
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
    """set / get / delete / status；keyring 不可用时自动落 DPAPI 加密文件。"""

    def __init__(self, *, service: str = "super-brain-v3") -> None:
        self._service = service
        self._path = user_dir() / "secrets.bin"
        try:
            import keyring  # noqa: F401  可选依赖，冻结包里可能没有
            self._keyring: Any = keyring
        except Exception:  # noqa: BLE001
            self._keyring = None

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
            self._path.write_bytes(_dpapi_encrypt(raw) if sys.platform == "win32" else raw)
            return True
        except OSError:
            return False

    def get(self, account: str) -> str:
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(self._service, account)
                if value:
                    return str(value)
            except Exception:  # noqa: BLE001
                pass
        return self._read_file().get(account) or os.environ.get("GITHUB_PAT") or ""

    def backend(self) -> str:
        if self._keyring is not None:
            try:
                if self._keyring.get_password(self._service, "_probe"):
                    return "keyring"
            except Exception:  # noqa: BLE001
                return "file"
        return "DPAPI 加密文件" if sys.platform == "win32" else "本地文件"

    def set(self, account: str, value: str) -> str:
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

    def status(self, accounts: list[str]) -> dict[str, Any]:
        """只回传「有没有值 / 用哪个后端」，绝不回传密钥明文。"""
        return {
            "backend": self.backend(),
            "accounts": {name: bool(self.get(name)) for name in accounts},
        }


_STORE: SecretStore | None = None


def default_secret_store() -> SecretStore:
    global _STORE
    if _STORE is None:
        _STORE = SecretStore()
    return _STORE


# ── 数据目录一键迁移 ────────────────────────────────────────────────────
def _pointer_path() -> Path:
    # 必须与 core.runtime_paths._data_pointer() 是同一个文件：
    # 用户目录（user_dir）同时被两者使用，且测试可用 SUPER_BRAIN_HOME 隔离。
    return user_dir() / "data_pointer.json"


def _pointer_target() -> str:
    try:
        payload = json.loads(_pointer_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(payload.get("path") or "") if isinstance(payload, dict) else ""


def clear_data_pointer() -> None:
    try:
        _pointer_path().unlink()
    except OSError:
        pass


def _tree_size(path: Path) -> tuple[int, int]:
    files = size = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                size += (Path(root) / name).stat().st_size
                files += 1
            except OSError:
                continue
    return files, size


def _active_data_root() -> Path:
    """六引擎真正落盘的目录。

    config.py 在启动时把 DATA_DIR = data_dir() 固化进 storage.*_path；
    运行时 data_dir() 可能因 pointer / 环境变量变化而不同。状态展示与迁移
    必须以引擎实际打开的路径为准，否则会搬错目录、甚至把真记忆覆盖掉。
    """
    try:
        from config import get_config

        return Path(get_config().storage.sqlite_path).expanduser().resolve().parent
    except Exception:  # noqa: BLE001
        return data_dir()


def data_status() -> dict[str, Any]:
    current = _active_data_root()
    files, size = _tree_size(current)
    return {
        "current": str(current),
        "pointer": _pointer_target(),
        "files": files,
        "size_mb": round(size / 1024 ** 2, 1),
        "env_override": os.environ.get("SUPER_BRAIN_DATA_DIR") or "",
    }


def migrate_data(target: str, *, move: bool = False) -> dict[str, Any]:
    """复制（默认）或搬移数据目录，并写下 pointer；**重启后端后生效**。

    默认保留原目录：复制期间六引擎仍在写，原目录始终是可靠回退。
    """
    source = _active_data_root()
    destination = Path(target.strip()).expanduser()
    if not target.strip():
        return {"ok": False, "message": "目标目录不能为空"}
    if destination.resolve() == source.resolve():
        return {"ok": False, "message": "目标目录与当前数据目录相同"}
    if source in destination.parents:
        return {"ok": False, "message": "目标目录不能位于当前数据目录内部"}
    # 逐文件 copy2 会覆盖目标同名文件；目标已有库时直接拒绝，避免把真记忆冲掉。
    existing_db = destination / "relational.db"
    if existing_db.is_file() and existing_db.stat().st_size > 0:
        return {
            "ok": False,
            "message": f"目标目录已有 relational.db，为防止覆盖真记忆已中止：{existing_db}。"
                       f"请先手动处理该目录，或换一个空目录再迁移。",
            "source": str(source), "target": str(destination),
        }
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "message": f"无法创建目标目录：{exc}"}
    copied = skipped = 0
    try:
        for root, dirs, names in os.walk(source):
            rel = Path(root).relative_to(source)
            out_dir = destination / rel
            out_dir.mkdir(parents=True, exist_ok=True)
            for name in names:
                src = Path(root) / name
                dst = out_dir / name
                try:
                    shutil.copy2(src, dst)
                    copied += 1
                except OSError:  # 被 Kùzu / LMDB 独占的锁文件跳过，不中断整批
                    skipped += 1
    except OSError as exc:
        return {"ok": False, "message": f"复制失败：{exc}"}
    _pointer_path().write_text(
        json.dumps({"path": str(destination), "ts_ms": int(time.time() * 1000)},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if move and skipped == 0 and copied > 0:
        # 仅在零跳过时才敢删除原目录；否则保留，避免数据丢失
        try:
            shutil.rmtree(source)
        except OSError:
            move = False
    files, size = _tree_size(destination)
    return {
        "ok": True, "from": str(source), "to": str(destination),
        "copied": copied, "skipped": skipped, "files": files,
        "size_mb": round(size / 1024 ** 2, 1),
        "moved": bool(move), "pointer_written": True,
        "restart_required": True,
        "message": f"已复制 {copied} 个文件（跳过占用中 {skipped} 个）到 {destination}；重启后端后生效",
    }
