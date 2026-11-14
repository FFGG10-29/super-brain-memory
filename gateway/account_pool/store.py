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
import subprocess
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
    """explicit > env WORKBUDDY_AUTH_FILE > %LOCALAPPDATA% > %APPDATA% 同路径。

    这是 WorkBuddy **桌面端**当前登录态活文件（一次仅一个账号）。多账号要靠
    `import_account_snapshot` 把每次切号后的登录态另存为 ~/.dsh/.xdpool/*.info；
    桌面端自己不产生历史快照文件（见 xdpool commandLogin 权威说明）。
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_path = os.environ.get("WORKBUDDY_AUTH_FILE", "")
    if env_path:
        candidates.append(Path(env_path))
    # ★ 显式指定（参数或 WORKBUDDY_AUTH_FILE）时绝不回退到真实桌面端活文件：
    #   否则测试/多实例把 env 指向空路径仍会抓到本机登录态，破坏隔离、污染账号数。
    if explicit or env_path:
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None
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


# ── 账号转移文件（xdpool 导入/导出，transfer.ts 移植）─────────────────────
_TRANSFER_WRAPPERS = ("accounts", "data", "records")


def parse_transfer_file(text: str) -> list[dict[str, Any]]:
    """解析转移文件：裸数组或 {accounts|data|records:[...]}（再包一层 data/result）。

    剥掉兄弟工具塞进来的 *_raw 快照（auth_raw 除外——它带 billing uin，
    是 re-import 收敛同一 vault id 的依据）。畸形文件报错时带上顶层形状，
    让用户能自行修复而不是瞎猜。
    """
    text = str(text or "").strip()
    if not text:
        raise AccountPoolError("转移文件为空")
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise AccountPoolError(f"转移文件不是 JSON：{exc}") from exc

    def records_of(node: Any, depth: int = 0) -> list[Any] | None:
        if isinstance(node, list):
            return node
        if not isinstance(node, dict):
            return None
        for key in _TRANSFER_WRAPPERS:
            value = node.get(key)
            if isinstance(value, list):
                return value
        if depth >= 1:
            return None
        for key in ("data", "result"):
            inner = records_of(node.get(key), depth + 1) if isinstance(node.get(key), dict) else None
            if inner is not None:
                return inner
        return None

    records = records_of(parsed)
    if records is None:
        shape = "数组" if isinstance(parsed, list) else (
            f"对象（keys={list(parsed)[:6]}）" if isinstance(parsed, dict) else type(parsed).__name__)
        raise AccountPoolError(
            f"转移文件须是账号记录数组；实际是 {shape}。"
            f"接受的包装键：{_TRANSFER_WRAPPERS}")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(records):
        if not isinstance(item, dict):
            raise AccountPoolError(f"转移文件第 {index + 1} 项不是账号对象")
        out.append({k: v for k, v in item.items()
                    if k == "auth_raw" or not str(k).endswith("_raw")})
    return out


def _region_of_domain(domain: str) -> str:
    lowered = str(domain or "").lower()
    if lowered.endswith(("workbuddy.ai", "codebuddy.ai")):
        return "global"
    if lowered.endswith(("workbuddy.cn", "codebuddy.cn", "copilot.tencent.com")):
        return "cn"
    return ""  # 未知：拒绝（域名必须能正面归类，防止误入别区金库）


def import_transfer(text: str, *, accounts_file: str = "") -> dict[str, int]:
    """导入转移记录：按 account_id 去重合并进 accounts.json，凭证保留原样。"""
    records = parse_transfer_file(text)
    path = Path(accounts_file) if accounts_file else (
        Path(os.environ.get("XDPOOL_ACCOUNTS_FILE", ""))
        if os.environ.get("XDPOOL_ACCOUNTS_FILE") else
        Path.home() / ".dsh" / ".xdpool" / "accounts.json")
    existing: dict[str, WorkBuddyCredential] = {}
    meta = load_meta(path)
    if path.is_file():
        try:
            for credential in discover_accounts(accounts_file=str(path)):
                existing[credential.account_id()] = credential
        except Exception:  # noqa: BLE001  已有文件解析失败也允许导入
            existing = {}
    added, skipped = 0, 0
    for record in records:
        access_token = str(record.get("access_token") or record.get("accessToken") or "")
        if not access_token:
            skipped += 1
            continue
        domain = str(record.get("domain") or "")
        region = _region_of_domain(domain)
        if not region:
            skipped += 1
            continue
        auth_raw = record.get("auth_raw") if isinstance(record.get("auth_raw"), dict) else {}
        account_block = auth_raw.get("account") if isinstance(auth_raw.get("account"), dict) else {}
        uin = str(account_block.get("uin") or record.get("uin") or "")
        uid = str(account_block.get("uid") or record.get("uid") or "")
        nickname = str(record.get("nickname") or account_block.get("nickname") or "")
        enterprise_id = str(record.get("enterprise_id")
                            or record.get("enterpriseId")
                            or account_block.get("enterpriseId") or "")
        expires_at = record.get("expiresAt") or record.get("expiresAtMs") or 0
        refresh_expires = record.get("refreshExpiresAt") or record.get("refreshExpiresAtMs") or 0
        credential = WorkBuddyCredential(
            access_token=access_token,
            refresh_token=str(record.get("refresh_token") or record.get("refreshToken") or ""),
            expires_at_ms=_to_ms(expires_at),
            refresh_expires_at_ms=_to_ms(refresh_expires),
            nickname=nickname, uin=uin, uid=uid,
            enterprise_id=enterprise_id, domain=domain, region=region,
            source_path=str(path))
        account_id = credential.account_id()
        if account_id in existing:
            existing[account_id] = credential  # 覆盖为新凭证（token 会滚动）
            continue
        existing[account_id] = credential
        added += 1
    ignored = {str(item) for item in meta.get("ignored", [])}
    save_accounts_atomic(
        path, list(existing.values()),
        ignored=ignored,
        disabled_models=meta.get("disabled_models"),
        default_model=meta.get("default_model", ""),
        model_prefs=meta.get("model_prefs"))
    return {"records": len(records), "added": added,
            "total": len(existing), "skipped": skipped}


# ── 登录态监听快照（多账号核心：换号登录先capture再被覆盖）─────────────────
def capture_auth_snapshot(*, accounts_file: str = "", auth_file: str = "") -> dict[str, Any]:
    """把**当前** WorkBuddy 桌面端登录态合并进 accounts.json（按 account_id 去重）。

    正确多账号流程（xdpool commandLogin 权威）：桌面端一次只登一个账号，
    你在桌面端手动切号后，每次调本函数把"当前账号"抓进池，旧账号因
    accounts.json 已存而保留 → 多账号并存。**浏览器登录不写桌面端凭证文件，
    因此无法用本函数捕获**——必须先让 WorkBuddy 桌面端登录该账号。
    """
    auth_path = discover_auth_file(auth_file)
    if auth_path is None or not auth_path.is_file():
        return {"captured": 0, "reason": "无桌面端登录态文件（浏览器登录不写入此处）"}
    try:
        credential = load_workbuddy_auth(auth_path)
    except Exception as exc:  # noqa: BLE001  加密/损坏不计入
        return {"captured": 0, "reason": f"登录态不可用：{exc}"}
    if not credential.access_token:
        return {"captured": 0, "reason": "空 token"}
    path = Path(accounts_file) if accounts_file else (
        Path(os.environ.get("XDPOOL_ACCOUNTS_FILE", ""))
        if os.environ.get("XDPOOL_ACCOUNTS_FILE") else
        Path.home() / ".dsh" / ".xdpool" / "accounts.json")
    existing: dict[str, WorkBuddyCredential] = {}
    meta = load_meta(path)
    if path.is_file():
        try:
            for item in discover_accounts(accounts_file=str(path)):
                existing[item.account_id()] = item
        except Exception:  # noqa: BLE001
            existing = {}
    account_id = credential.account_id()
    changed = account_id not in existing or \
        existing[account_id].access_token != credential.access_token
    existing[account_id] = credential
    if changed:
        save_accounts_atomic(
            path, list(existing.values()),
            ignored=meta.get("ignored"),
            disabled_models=meta.get("disabled_models"),
            default_model=meta.get("default_model", ""),
            model_prefs=meta.get("model_prefs"))
    return {"captured": 1 if changed else 0,
            "total": len(existing), "account_id": account_id,
            "changed": changed}


def discover_accounts(*, accounts_file: str = "", auth_file: str = "",
                      extra_auth_dirs: list[str] | None = None) -> list[WorkBuddyCredential]:
    """xdpool accounts.json + WorkBuddy 登录态 + OAuth vault，按 account_id 去重。

    `extra_auth_dirs` 是额外的凭证目录（每个 *.info 走 load_workbuddy_auth）——
    默认纳入 wbmod 号池网关的 vault（D:/workbuddy/wbmod/vendor/xdpool-gateway/vault），
    这样 wbmod 浏览器 OAuth 扫码入池的账号，超级大脑 rescan 也能直接读到，
    两边共享同一份凭证，不必各扫各的。结构已实测兼容（同名 auth/account 嵌套形）。
    """
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
        try:
            credential = load_workbuddy_auth(auth_path)
        except Exception:  # noqa: BLE001  登录态损坏不影响 xdpool 已存账号
            credential = None
        if credential is not None and credential.access_token \
                and credential.account_id() not in seen:
            seen.add(credential.account_id())
            found.append(credential)

    # ★ OAuth vault 目录（wmob 浏览器扫码入池的凭证）：与 wbmod 号池网关共享。
    dirs = list(extra_auth_dirs or [])
    if extra_auth_dirs is None:
        # 环境变量优先（测试隔离：设为空串可彻底关闭 vault 扫描，避免污染真实凭证）。
        env_dirs = os.environ.get("XDPOOL_VAULT_DIRS", "__DEFAULT__")
        if env_dirs == "__DEFAULT__":
            default_vault = Path.home() / ".workbuddy" / "xdpool-vault"  # 可配置兜底
            wbmod_vault = Path("D:/workbuddy/wbmod/vendor/xdpool-gateway/vault")
            dirs = [str(wbmod_vault), str(default_vault)]
        else:
            dirs = [d for d in env_dirs.split(";") if d.strip()]
    for d in dirs:
        vault = Path(d)
        if not vault.is_dir():
            continue
        for info in sorted(vault.glob("*.info")):
            try:
                credential = load_workbuddy_auth(info)
            except Exception:  # noqa: BLE001  单个损坏文件不影响其它账号
                continue
            if not credential.access_token or credential.account_id() in seen:
                continue
            seen.add(credential.account_id())
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


def _restrict_windows_acl(path: Path) -> None:
    """Windows 上把凭证文件 ACL 收紧为「仅当前用户 + SYSTEM」，兑现文档 0600 承诺。

    POSIX 的 os.chmod(0600) 在 Windows 上只切只读位、完全不碰 DACL——
    实测 accounts.json 继承 SYSTEM + BUILTIN\\Administrators + 当前用户全控制，
    管理员组可读明文 accessToken/refreshToken。这里用 icacls 断继承、只留当前用户
    与 SYSTEM（服务/备份需要），失败一律静默（不让 ACL 问题阻断凭证落盘）。

    用 /inheritance:r 断开父目录继承；/grant:r 替换为显式授权（非追加）。
    """
    if os.name != "nt":
        return
    try:
        # 当前用户（DOMAIN\\USER 或 COMPUTER\\USER）；icacls 接受 *SIDNAME 语法更稳
        user = os.environ.get("USERNAME", "")
        domain = os.environ.get("USERDOMAIN", "")
        principal = f"{domain}\\{user}" if domain and user else (user or "*S-1-1-0")
        # 先断继承，再显式授予当前用户与 SYSTEM 完全控制；移除其它 DACL 条目
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r",
             f"{principal}:(F)", "*S-1-5-18:(F)"],
            capture_output=True, timeout=15, check=False,
        )
    except Exception:  # noqa: BLE001  ACL 收紧失败不阻断凭证写入
        pass


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
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # POSIX 0600（Windows 上仅切只读位）
    _restrict_windows_acl(path)                   # Windows 真收紧 DACL（P0-2）
