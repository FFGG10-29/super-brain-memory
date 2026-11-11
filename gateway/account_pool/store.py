"""gateway.account_pool.store —— xdpool accounts.json 与 WorkBuddy 登录态读写。

照抄事实来源：§16.14「号池网关 Python 重写」（dsh-xdpool v2.6.4 /
dsh-connect-workbuddy v5.21.3，均 MIT）。首发只支持明文登录态，
``$wbEncrypted`` 加密文件明确拒绝（提示"请用 xdpool 导入"）。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "AccountPoolError",
    "WorkBuddyCredential",
    "account_id_of",
    "discover_accounts",
    "discover_auth_file",
    "load_workbuddy_auth",
    "load_ignored",
    "save_accounts_atomic",
]

_CN_CHAT_BASE = "https://copilot.tencent.com"
_GLOBAL_CHAT_BASE = "https://www.workbuddy.ai"


class AccountPoolError(RuntimeError):
    """号池域错误：存储、刷新、上游协议。"""


@dataclass
class WorkBuddyCredential:
    """单个 WorkBuddy 账号的凭据与运行时状态。"""

    access_token: str = ""
    refresh_token: str = ""
    expires_at_ms: int = 0
    refresh_expires_at_ms: int = 0
    last_refresh_at_ms: int = 0
    nickname: str = ""
    uin: str = ""
    uid: str = ""
    enterprise_id: str = ""
    domain: str = ""
    region: str = "cn"  # "cn" | "global"
    source_path: str = ""
    cooldowns: dict[str, float] = field(default_factory=dict)  # model -> 冷却截止(epoch秒)
    dead_until: float = 0.0  # 登录态死亡截止（epoch 秒）

    def account_id(self) -> str:
        return account_id_of(self.uin, self.uid, self.nickname)

    def label(self) -> str:
        return self.nickname or self.uid or self.uin or self.account_id()

    def chat_base(self) -> str:
        return _GLOBAL_CHAT_BASE if self.region == "global" else _CN_CHAT_BASE


def account_id_of(uin: str, uid: str, nickname: str) -> str:
    """sha256(b"workbuddy" + b"\\x00" + key) 前 16 hex；key = uin or uid or nickname。"""
    key = uin or uid or nickname
    if not key:
        raise AccountPoolError("account_id_of: uin/uid/nickname 均为空")
    seed = "workbuddy" + chr(0) + key  # 分隔符用 chr(0) 构造，不写 NUL 字面量
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _to_ms(value: Any) -> int:
    """expiresAt(Ms)/lastRefreshTime 归一到毫秒；秒级(<1e12)自动放大。"""
    if value in (None, ""):
        return 0
    number = int(value)
    if 0 < number < 10**12:
        number *= 1000
    return number


def _decrypt_if_encrypted(data: dict, source: str) -> dict:
    """含 $wbEncrypted 字段时经 WorkBuddy 桌面端密钥解密（atrest）；失败明确报错。"""

    def has_wrapper(node: Any) -> bool:
        if isinstance(node, dict):
            return "$wbEncrypted" in node or any(has_wrapper(v) for v in node.values())
        if isinstance(node, list):
            return any(has_wrapper(item) for item in node)
        return False

    if not has_wrapper(data):
        return data
    from gateway.account_pool import atrest

    try:
        decrypted = atrest.decrypt_json_tree(data)
    except atrest.AtRestError as exc:
        raise AccountPoolError(
            f"{source}: 登录态已加密且解密失败——{exc}。"
            "备选：用 xdpool 导出明文 accounts.json 后导入。") from exc
    if not isinstance(decrypted, dict):
        raise AccountPoolError(f"{source}: 解密结果不是对象")
    return decrypted


def load_workbuddy_auth(path: Path) -> WorkBuddyCredential:
    """读 workbuddy-desktop.info；接受嵌套形 {"auth":{...},"account":{...}} 与扁平形。"""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AccountPoolError(f"{path}: 登录态 JSON 顶层不是对象")
    data = _decrypt_if_encrypted(data, str(path))
    auth = data.get("auth") if isinstance(data.get("auth"), dict) else {}
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    flat = data if not auth else {}

    def pick(*names: str) -> str:
        for source in (auth, account, flat):
            if not isinstance(source, dict):
                continue
            for name in names:
                value = source.get(name)
                if value not in (None, ""):
                    return str(value)
        return ""

    region = pick("region").lower()
    return WorkBuddyCredential(
        access_token=pick("accessToken", "access_token"),
        refresh_token=pick("refreshToken", "refresh_token"),
        expires_at_ms=_to_ms(pick("expiresAtMs", "expiresAt")),
        refresh_expires_at_ms=_to_ms(pick("refreshExpiresAtMs", "refreshExpiresAt")),
        last_refresh_at_ms=_to_ms(pick("lastRefreshTime", "lastRefreshAtMs")),
        nickname=pick("nickname"),
        uin=pick("uin"),
        uid=pick("uid"),
        enterprise_id=pick("enterpriseId", "enterprise_id"),
        domain=pick("domain"),
        region=region if region in ("cn", "global") else "cn",
        source_path=str(path),
    )


def discover_auth_file(explicit: str = "") -> Path | None:
    """explicit > env WORKBUDDY_AUTH_FILE > %LOCALAPPDATA% > %APPDATA% 同路径。"""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_path = os.environ.get("WORKBUDDY_AUTH_FILE", "")
    if env_path:
        candidates.append(Path(env_path))
    rel = (Path("CodeBuddyExtension") / "Data" / "Public" / "auth"
           / "workbuddy-desktop.info")
    for var in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(var, "")
        if base:
            candidates.append(Path(base) / rel)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _credential_from_xdpool(entry: dict, source: str) -> WorkBuddyCredential:
    cred = entry.get("credential") or {}
    return WorkBuddyCredential(
        access_token=str(cred.get("accessToken") or ""),
        refresh_token=str(cred.get("refreshToken") or ""),
        expires_at_ms=_to_ms(cred.get("expiresAtMs") or cred.get("expiresAt")),
        refresh_expires_at_ms=_to_ms(
            cred.get("refreshExpiresAtMs") or cred.get("refreshExpiresAt")),
        last_refresh_at_ms=_to_ms(
            cred.get("lastRefreshAtMs") or cred.get("lastRefreshTime")),
        nickname=str(entry.get("label") or cred.get("nickname") or ""),
        uin=str(cred.get("uin") or ""),
        uid=str(cred.get("uid") or ""),
        enterprise_id=str(cred.get("enterpriseId") or ""),
        domain=str(cred.get("domain") or ""),
        region=str(cred.get("region") or "cn"),
        source_path=source,
    )


def discover_accounts(*, accounts_file: str = "", auth_file: str = "") -> list[WorkBuddyCredential]:
    """xdpool accounts.json + WorkBuddy 登录态，按 account_id 去重（先出现者优先）。"""
    found: list[WorkBuddyCredential] = []
    seen: set[str] = set()

    if accounts_file:
        xdpool_path = Path(accounts_file)
    else:
        # 测试隔离出口：XDPOOL_ACCOUNTS_FILE 重定向默认 accounts.json
        env_file = os.environ.get("XDPOOL_ACCOUNTS_FILE", "")
        xdpool_path = Path(env_file) if env_file else \
            Path.home() / ".dsh" / ".xdpool" / "accounts.json"
    if xdpool_path.is_file():
        data = json.loads(xdpool_path.read_text(encoding="utf-8"))
        for entry in data.get("accounts") or []:
            if not isinstance(entry, dict):
                continue
            credential = _credential_from_xdpool(entry, str(xdpool_path))
            key = str(entry.get("id") or "") or credential.account_id()
            if key in seen:
                continue
            seen.add(key)
            found.append(credential)

    auth_path = discover_auth_file(auth_file)
    if auth_path is not None:
        credential = load_workbuddy_auth(auth_path)
        if credential.account_id() not in seen:
            found.append(credential)
    return found


def load_meta(path: Path) -> dict[str, Any]:
    """accounts.json 顶层元数据（ignored / disabled_models / default_model）。"""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    meta: dict[str, Any] = {}
    if isinstance(data.get("ignored"), list):
        meta["ignored"] = [str(item) for item in data["ignored"] if item]
    if isinstance(data.get("disabled_models"), list):
        meta["disabled_models"] = [str(item) for item in data["disabled_models"] if item]
    if isinstance(data.get("default_model"), str):
        meta["default_model"] = data["default_model"]
    if isinstance(data.get("model_prefs"), dict):
        meta["model_prefs"] = {
            str(k): dict(v) for k, v in data["model_prefs"].items()
            if isinstance(v, dict)}
    return meta


def load_ignored(path: Path) -> set[str]:
    """accounts.json 顶层 ignored 列表（「移出号池」的账号 id；缺失视为空）。"""
    return {str(item) for item in load_meta(path).get("ignored", [])}


def save_accounts_atomic(path: Path, accounts: Iterable[WorkBuddyCredential],
                         ignored: Iterable[str] | None = None,
                         disabled_models: Iterable[str] | None = None,
                         default_model: str = "",
                         model_prefs: dict[str, dict] | None = None) -> None:
    """tmp 写 + os.replace + 0600（xdpool accounts.json 结构）。"""
    path = Path(path)
    now_ms = int(time.time() * 1000)
    entries = []
    for account in accounts:
        entries.append({
            "id": account.account_id(),
            "label": account.label(),
            "credential": {
                "accessToken": account.access_token,
                "refreshToken": account.refresh_token,
                "expiresAtMs": account.expires_at_ms,
                "refreshExpiresAtMs": account.refresh_expires_at_ms,
                "lastRefreshAtMs": account.last_refresh_at_ms,
                "nickname": account.nickname,
                "uin": account.uin,
                "uid": account.uid,
                "enterpriseId": account.enterprise_id,
                "domain": account.domain,
                "region": account.region,
            },
            "savedAtMs": now_ms,
        })
    payload: dict[str, Any] = {"version": 1, "savedAtMs": now_ms, "accounts": entries}
    if ignored is not None:
        payload["ignored"] = sorted({str(item) for item in ignored if item})
    if disabled_models is not None:
        payload["disabled_models"] = sorted({str(item) for item in disabled_models if item})
    if default_model:
        payload["default_model"] = str(default_model)
    if model_prefs is not None:
        payload["model_prefs"] = {
            str(k): dict(v) for k, v in model_prefs.items() if isinstance(v, dict)}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
