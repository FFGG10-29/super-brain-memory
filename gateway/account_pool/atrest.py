"""gateway.account_pool.atrest —— WorkBuddy 桌面端 5.6.0+ 登录态字段解密。

移植自 dsh-connect-workbuddy/src/at-rest.ts（MIT）。登录态文件里
accessToken/refreshToken 是 ``{"$wbEncrypted":1,"envelope":"<b64 JSON>"}``，
envelope = {suite, keyId, nonce, authTag, ciphertext}，AES-256-GCM（12B nonce、
16B tag），AAD 是对 scheme/suite/keyId/framing 的长度前缀 transcript。

字段密钥不是用户机密：它是编译进应用原生模块的构建时常量，应用经
``loggerGet()`` 返回其 base64 字符串，对该字符串（不是解码字节）做 sha256
得到 32 字节 key；keyId = sha256(key) 前 16 hex。获取方式：把桌面 exe 当
Node 跑（ELECTRON_RUN_AS_NODE=1）调 ``process._linkedBinding(
'electron_browser_workbuddy_storage').loggerGet()``。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

__all__ = [
    "find_workbuddy_executable",
    "derive_at_rest_key",
    "open_encrypted_field",
    "decrypt_json_tree",
    "AtRestError",
]


class AtRestError(RuntimeError):
    """登录态解密域错误。"""


_APP_EXECUTABLE_NAMES = ("WorkBuddy.exe", "WorkBuddyAI.exe")
_WINDOWS_APP_SUBDIRECTORIES = (
    ("Programs", "WorkBuddy"),
    ("WorkBuddy",),
    ("Programs", "WorkBuddyAI"),
    ("WorkBuddyAI",),
)

# ── envelope framing（照抄 at-rest.ts 的常量表）───────────────────────────
_AAD_DOMAIN = b"WB-AAD\x00"
_STANDARD_FORMAT_ID_FIELD = "WBEV1"
_SYMMETRIC_SCHEME = "sym-v1"
_FRAMING_CODE_FIELD = 2


def _encode_uint32(value: int) -> bytes:
    return value.to_bytes(4, "big")


def _encode_length_prefixed(value: str) -> bytes:
    raw = value.encode("utf-8")
    return _encode_uint32(len(raw)) + raw


def _field_aad(key_id: str, suite: int) -> bytes:
    if len(key_id) != 16 or any(c not in "0123456789abcdef" for c in key_id):
        raise AtRestError("workbuddy: envelope keyId is malformed")
    return b"".join((
        _AAD_DOMAIN,
        b"\x01",
        _encode_length_prefixed(_STANDARD_FORMAT_ID_FIELD),
        _encode_length_prefixed(_SYMMETRIC_SCHEME),
        _encode_uint32(suite),
        _encode_length_prefixed(key_id),
        bytes([_FRAMING_CODE_FIELD]),
        b"\x00",  # encodeOptionalUint64(undefined)
        b"\x00",  # final === undefined
    ))


# ── 桌面端 exe 定位（env > 默认布局 > 注册表 DisplayIcon）──────────────────
def _registry_app_executable() -> str | None:
    try:
        import winreg  # noqa: PLC0415  仅 Windows 使用
    except ImportError:
        return None
    hives = (
        winreg.HKEY_CURRENT_USER,
        winreg.HKEY_LOCAL_MACHINE,
    )
    roots = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    lowered = {name.lower() for name in _APP_EXECUTABLE_NAMES}
    for hive in hives:
        for root in roots:
            try:
                key = winreg.OpenKey(hive, root)
            except OSError:
                continue
            with key:
                index = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        sub = winreg.OpenKey(key, subkey_name)
                    except OSError:
                        continue
                    with sub:
                        try:
                            display_name, _ = winreg.QueryValueEx(sub, "DisplayName")
                        except OSError:
                            continue
                        if "workbuddy" not in str(display_name).lower():
                            continue
                        for value_name in ("DisplayIcon", "InstallLocation"):
                            try:
                                value, _ = winreg.QueryValueEx(sub, value_name)
                            except OSError:
                                continue
                            text = str(value).strip().strip('"')
                            text = text.split(",0")[0].strip()
                            base = Path(text).name.lower()
                            if base in lowered and Path(text).is_file():
                                return text
                            if value_name == "InstallLocation":
                                for name in _APP_EXECUTABLE_NAMES:
                                    candidate = Path(text) / name
                                    if candidate.is_file():
                                        return str(candidate)
    return None


def find_workbuddy_executable() -> str | None:
    """env WORKBUDDY_APP_EXECUTABLE > 默认安装布局 > 卸载注册表。"""
    override = os.environ.get("WORKBUDDY_APP_EXECUTABLE", "").strip()
    if override and Path(override).is_file():
        return override
    bases = [os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", ""),
             os.environ.get("ProgramFiles(x86)", "")]
    for base in bases:
        if not base:
            continue
        for subdirectory in _WINDOWS_APP_SUBDIRECTORIES:
            for name in _APP_EXECUTABLE_NAMES:
                candidate = Path(base).joinpath(*subdirectory, name)
                if candidate.is_file():
                    return str(candidate)
    return _registry_app_executable()


# ── 密钥获取与解密 ────────────────────────────────────────────────────────
_KEY_SOURCE = (
    "try{process.stdout.write(process._linkedBinding("
    "'electron_browser_workbuddy_storage').loggerGet())}"
    "catch(e){process.exitCode=3;process.stderr.write(String(e&&e.message||e))}"
)

_cached_key: bytes | None = None


def _fetch_key_payload(executable: str) -> str:
    env = dict(os.environ)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        proc = subprocess.run(
            [executable, "-e", _KEY_SOURCE],
            capture_output=True, timeout=10, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AtRestError(f"workbuddy: 桌面端密钥获取失败：{exc}") from exc
    payload = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not payload:
        detail = proc.stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
        raise AtRestError(f"workbuddy: 桌面端未提供 at-rest 密钥（{detail}）")
    return payload


def derive_at_rest_key(payload_json: str) -> bytes:
    try:
        payload = json.loads(payload_json)
    except ValueError as exc:
        raise AtRestError("workbuddy: at-rest key payload 非 JSON") from exc
    secret = payload.get("atRestSecretKey") if isinstance(payload, dict) else None
    if not isinstance(secret, str) or not secret:
        raise AtRestError("workbuddy: at-rest key payload 缺少 atRestSecretKey")
    # 应用端 hash 的是 base64 字符串本身，不是解码后的字节
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _load_key() -> bytes:
    global _cached_key
    if _cached_key is not None:
        return _cached_key
    executable = find_workbuddy_executable()
    if executable is None:
        raise AtRestError(
            "未找到 WorkBuddy 桌面端（解密需要它的原生密钥）。"
            "可设 WORKBUDDY_APP_EXECUTABLE 指向 WorkBuddy.exe")
    _cached_key = derive_at_rest_key(_fetch_key_payload(executable))
    return _cached_key


def derive_at_rest_key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def open_encrypted_field(field: dict[str, Any], key: bytes | None = None) -> str:
    """打开一个 {"$wbEncrypted":1,"envelope":...} 字段，返回明文字符串。"""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    envelope_b64 = field.get("envelope")
    if not isinstance(envelope_b64, str) or not envelope_b64:
        raise AtRestError("workbuddy: 加密字段缺少 envelope（畸形包装）")
    try:
        envelope = json.loads(base64.b64decode(envelope_b64).decode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise AtRestError("workbuddy: envelope 非 JSON") from exc
    if not isinstance(envelope, dict):
        raise AtRestError("workbuddy: envelope 不是对象")
    suite = envelope.get("suite")
    key_id = envelope.get("keyId")
    nonce_b64 = envelope.get("nonce")
    tag_b64 = envelope.get("authTag")
    ciphertext_b64 = envelope.get("ciphertext")
    if not all(isinstance(v, str) for v in (key_id, nonce_b64, tag_b64, ciphertext_b64)) \
            or not isinstance(suite, int):
        raise AtRestError("workbuddy: envelope 缺少 suite/keyId/nonce/authTag/ciphertext")

    key = key if key is not None else _load_key()
    expected = derive_at_rest_key_id(key)
    if key_id != expected:
        raise AtRestError(
            f"workbuddy: 字段属于 key {key_id}，当前密钥是 {expected}")
    try:
        aes = AESGCM(key)
        plaintext = aes.decrypt(
            base64.b64decode(nonce_b64),
            base64.b64decode(ciphertext_b64) + base64.b64decode(tag_b64),
            _field_aad(key_id, suite),
        )
    except InvalidTag as exc:
        raise AtRestError("workbuddy: GCM 校验失败（transcript/密钥不符）") from exc
    return plaintext.decode("utf-8")


def decrypt_json_tree(node: Any, key: bytes | None = None) -> Any:
    """递归解 JSON 树里所有 $wbEncrypted 包装字段；畸形包装直接报错不静默。"""
    if isinstance(node, dict):
        if "$wbEncrypted" in node:
            return open_encrypted_field(node, key)
        return {str(k): decrypt_json_tree(v, key) for k, v in node.items()}
    if isinstance(node, list):
        return [decrypt_json_tree(item, key) for item in node]
    return node
