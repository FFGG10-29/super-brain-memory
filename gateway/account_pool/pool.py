"""gateway.account_pool.pool —— 号池聚合客户端（照抄 §16.14 协议硬门）。

端点、头集合、指纹常量、mint 算法逐字照抄 dsh-xdpool v2.6.4 /
dsh-connect-workbuddy v5.21.3（均 MIT）。transport 可注入，测试零网络。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from gateway.account_pool import store
from gateway.account_pool.store import AccountPoolError, WorkBuddyCredential

__all__ = [
    "AccountPool",
    "AccountPoolError",
    "CLIENT_UA",
    "CONVERSATION_STABLE_SALT",
    "FREE_BASE",
    "FREE_CLIENT_UA",
    "PoolClient",
    "PoolConfig",
    "WorkBuddyCredential",
    "conversation_identity_of",
    "mint_request_id",
    "mint_session_id",
]

CONVERSATION_STABLE_SALT = "conversation"
CLIENT_UA = "CLI/2.63.2 CodeBuddy/2.63.2"
FREE_CLIENT_UA = "opencode/latest/2.0.26/cli"
FREE_BASE = "https://opencode.ai"

# 免密 lane 四连同值 session 头（§16.14：会话头四连同值；绝不带 x-opencode-request）。
_FREE_SESSION_HEADERS = (
    "x-opencode-session-id",
    "x-opencode-conversation-id",
    "x-opencode-request-id",
    "x-opencode-message-id",
)

Transport = Callable[[str, str, dict[str, str], bytes], tuple[int, dict, bytes]]


@dataclass
class PoolConfig:
    """号池配置块（config 的 pool 节；getattr 宽松读取，缺省即安全值）。"""

    accounts_file: str = ""
    auth_file: str = ""
    default_model: str = ""
    max_attempts: int = 3
    request_timeout_sec: float = 20.0
    credential_dead_sec: float = 3600.0
    free_lane: bool = False


class _RateLimited(AccountPoolError):
    """429：已写 (account, model) 冷却，调用方换号重试。"""


class _SessionDead(AccountPoolError):
    """401/403 / 12153 登录态失效：已写 dead_until，调用方换号重试。"""


class _OutOfCredit(AccountPoolError):
    """14018 / 欠费文案：账号额度耗尽，换号可救（本号当日别再试）。"""


# ── 上游业务码分类（照抄 dsh-connect-workbuddy v5.21.3 / upstream.ts，MIT）──
# 关键：HTTP 状态只是外壳，真正的故障语义在响应体 business code 里。
# 把 14003（模型忙，全局，冷模型 2 分钟）误当 6004（账号限流，冷账号 30 分钟）
# 会让整池被一个模型小抖动冷半小时——wbmod issue #56 的坑，这里一次性修对。
_HARD_CREDIT_MARKERS = (
    "insufficient credit", "no credit", "credit exhausted", "credits exhausted",
    "out of credit", "quota exceeded", "quota exhaust", "payment required",
    "额度已用尽", "积分不足", "余额不足",
)
_SESSION_DEAD_MARKERS = ("offline user session not found", "12153")
_MODEL_BUSY_CODE = 14003
_CREDITS_EXHAUSTED_CODE = 14018
_RATE_LIMITED_CODE = 6004
_MODEL_BUSY_COOLDOWN_SEC = 120.0        # 上游自述"约 1 分钟"，取 2 分钟跨过突发
_RETRY_TIME_RE = re.compile(
    r"(?:将在\s*)?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*(?:UTC[+\-]\d+|重置|reset)",
    re.IGNORECASE,
)


def _business_code(body: bytes) -> int | None:
    """从响应体取上游业务码，只认 code / error.data.code / data.code 三种形状。"""
    try:
        payload = json.loads(body.decode("utf-8", "replace") or "{}")
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    # 覆盖实测三种形状 + 一层嵌套：code / data.code / error.code / error.data.code /
    # data.error.code。按"钱包类优先"由调用方排序，这里只做纯提取（最浅命中先返回）。
    holders = (payload, data, error,
               error.get("data") if isinstance(error.get("data"), dict) else {},
               data.get("error") if isinstance(data.get("error"), dict) else {})
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        for key in ("code", "businessCode"):
            value = holder.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
    return None


def _reset_at_ms(body: bytes) -> float | None:
    """从响应体文案解析上游给出的恢复时间（epoch 秒）；上游没写就返回 None，不编造。"""
    text = body.decode("utf-8", "replace")
    match = _RETRY_TIME_RE.search(text)
    if not match:
        return None
    try:
        # 文案是本地时间（UTC+8）；本机时区即北京时间，按本地解释
        stamp = time.mktime(time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"))
        return stamp if stamp > time.time() else None
    except (ValueError, OverflowError):
        return None


# ---- free-upstream.ts:179-193 的单调计数器（照抄：降序计数器硬门） ----
_MINT_LOCK = threading.Lock()
_MINT_LAST_STAMP = 0
_MINT_SEQ = 0

_BASE62 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def _base62_suffix(length: int) -> str:
    number = uuid.uuid4().int
    chars = []
    for _ in range(length):
        chars.append(_BASE62[number % 62])
        number //= 62
    return "".join(chars)


def _mint_id(prefix: str, timestamp_ms: int | None) -> str:
    global _MINT_LAST_STAMP, _MINT_SEQ
    stamp = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    with _MINT_LOCK:
        if stamp <= _MINT_LAST_STAMP:
            stamp = _MINT_LAST_STAMP
            _MINT_SEQ += 1
            if _MINT_SEQ > 0x3F:  # 硬门：(~seq)&0xFFF 须落 0xfc0..0xfff -> seq <= 0x3f
                _MINT_SEQ = 0
                _MINT_LAST_STAMP += 1
                stamp = _MINT_LAST_STAMP
        else:
            _MINT_LAST_STAMP = stamp
            _MINT_SEQ = 0
        stamp, seq = _MINT_LAST_STAMP, _MINT_SEQ
    value = ~(stamp * 0x1000 + seq) & 0xFFFFFFFFFFFF  # 6 字节
    return prefix + format(value, "012x") + _base62_suffix(14)


def mint_session_id(timestamp_ms: int | None = None) -> str:
    """ses_ + 6 字节大端 hex(12) + 14 位 base62，匹配 ses_ 指纹正则。"""
    return _mint_id("ses_", timestamp_ms)


def mint_request_id(timestamp_ms: int | None = None) -> str:
    """同 mint_session_id，前缀 msg_。"""
    return _mint_id("msg_", timestamp_ms)


def conversation_identity_of(conversation_key: str) -> tuple[str, str, str]:
    """(X-Conversation-ID, X-Conversation-Request-ID, acp-connection-id)。

    sha256(f"conversation:{suffix}:{base}")，hex 切 8-4-4-4-12 UUID 形状；
    不设 version/variant 位。conversation_key 为空时用 uuid4 当 base。
    """
    base = conversation_key or str(uuid.uuid4())
    identities: list[str] = []
    for suffix in ("id", "request", "acp"):
        digest = hashlib.sha256(
            f"{CONVERSATION_STABLE_SALT}:{suffix}:{base}".encode("utf-8")
        ).hexdigest()
        identities.append(
            f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}"
            f"-{digest[16:20]}-{digest[20:32]}")
    return identities[0], identities[1], identities[2]


def _to_ms(value: Any) -> int:
    if value in (None, ""):
        return 0
    number = int(value)
    if 0 < number < 10**12:
        number *= 1000
    return number


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes,
                      timeout: float) -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也按 (status, headers, body) 返回
        return exc.code, dict(exc.headers.items()), exc.read()


def _header(resp_headers: dict, *names: str) -> str:
    for name in names:
        if resp_headers.get(name):
            return str(resp_headers[name])
    lower = {str(k).lower(): str(v) for k, v in resp_headers.items()}
    for name in names:
        if lower.get(name.lower()):
            return lower[name.lower()]
    return ""


def _snippet(body: bytes, limit: int = 200) -> str:
    return body.decode("utf-8", "replace")[:limit].strip()


def _extract_content(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    choice = choices[0] or {}
    delta = choice.get("delta") or {}
    message = choice.get("message") or {}
    return str(delta.get("content") or message.get("content") or "")


def _parse_chat_payload(body: bytes, resp_headers: dict) -> str:
    """兼容 SSE（逐行 data: {...} 聚合 delta.content 到 [DONE]）与非流式 JSON。"""
    text = body.decode("utf-8", "replace")
    content_type = _header(resp_headers, "Content-Type").lower()
    if "text/event-stream" in content_type or text.lstrip().startswith("data:"):
        parts: list[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                payload = json.loads(data)
            except ValueError:
                continue
            parts.append(_extract_content(payload))
        return "".join(parts)
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise AccountPoolError(f"chat 响应既非 SSE 也非 JSON: {text[:200]!r}") from exc
    return _extract_content(payload)


class AccountPool:
    """多账号额度池：discover / token 刷新 / chat / failover / 免密 lane。"""

    def __init__(self, config: Any, *, transport: Transport | None = None) -> None:
        self.config = config
        self.accounts: list[WorkBuddyCredential] = []
        # 「移出号池」清单：账号凭证保留在 accounts.json，但不再参与 failover
        self.ignored: set[str] = set()
        # 已服务计数（每账号成功 chat 次数，xdpool 面板「已服务」列）
        self.served: dict[str, int] = {}
        # 用户禁用的模型 id（目录里不展示、不参与 default）
        self.disabled_models: set[str] = set()
        # per-model 偏好：{id: {enabled, ctx_budget, image, thinking}}
        # thinking ∈ off/minimal/low/medium/high/xhigh/max（None=上游默认）
        self.model_prefs: dict[str, dict[str, Any]] = {}
        # 免密 lane 开关：config 可能是 BrainConfig（free_lane 在 .account_pool 上）
        # 也可能直接是 AccountPoolConfig——两种都兼容
        lane_source = getattr(config, "account_pool", config)
        self.free_lane_enabled = bool(getattr(lane_source, "free_lane", False))
        # ★ 默认模型必须兼容两种配置形态：直接 AccountPoolConfig（裸用）或 BrainConfig
        #   （model_router 装配时传整个 config，default_model 在 .account_pool 子块上）。
        #   此前只从顶层读 → 拿空串 → 上游 400 → 自动路由的号池分支永远失败。
        self._default_model = str(
            getattr(lane_source, "default_model", "")
            or getattr(config, "default_model", "") or "")
        self._timeout = float(getattr(lane_source, "request_timeout_sec",
                                       getattr(config, "request_timeout_sec", 20.0)))
        self._max_attempts = int(getattr(lane_source, "max_attempts",
                                         getattr(config, "max_attempts", 3)))
        self._credential_dead_sec = float(getattr(lane_source, "credential_dead_sec",
                                                  getattr(config, "credential_dead_sec", 3600.0)))
        if transport is not None:
            self._transport = transport
        else:
            def transport(method, url, headers, body, _timeout=self._timeout):
                return _urllib_transport(method, url, headers, body, _timeout)
            self._transport = transport

    # -- 生命周期 ---------------------------------------------------------

    def discover(self) -> None:
        self.accounts = store.discover_accounts(
            accounts_file=getattr(self.config, "accounts_file", "") or "",
            auth_file=getattr(self.config, "auth_file", "") or "")
        self._load_ignored()
        self._ensure_auth_watch()

    # ── 登录态监听（多账号自动入库：换号登录先快照再被覆盖）───────────────
    def _ensure_auth_watch(self) -> None:
        if getattr(self, "_auth_watch_started", False):
            return
        self._auth_watch_started = True
        thread = threading.Thread(target=self._auth_watch_loop,
                                  name="pool-auth-watch", daemon=True)
        thread.start()

    def _auth_watch_loop(self) -> None:
        """每 3s 看登录态文件指纹；变化 = 用户换了号登录 → 快照进 accounts.json。"""
        import time as _time

        last_fingerprint = ""
        while True:
            _time.sleep(3.0)
            try:
                auth_path = store.discover_auth_file(
                    getattr(self.config, "auth_file", "") or "")
                if auth_path is None or not auth_path.is_file():
                    continue
                stat = auth_path.stat()
                fingerprint = f"{auth_path}:{stat.st_mtime_ns}:{stat.st_size}"
                if fingerprint == last_fingerprint:
                    continue
                if last_fingerprint == "":
                    last_fingerprint = fingerprint  # 首轮只记基线
                    continue
                last_fingerprint = fingerprint
                result = store.capture_auth_snapshot(
                    accounts_file=getattr(self.config, "accounts_file", "") or "",
                    auth_file=getattr(self.config, "auth_file", "") or "")
                if result.get("captured"):
                    self.discover()
            except Exception:  # noqa: BLE001  监听失败静默重试
                continue

    def _accounts_path(self) -> Path:
        raw = getattr(self.config, "accounts_file", "") or ""
        return Path(raw) if raw else Path.home() / ".dsh" / ".xdpool" / "accounts.json"

    def _load_ignored(self) -> None:
        try:
            meta = store.load_meta(self._accounts_path())
            self.ignored = {str(item) for item in meta.get("ignored", [])}
            self.disabled_models = {str(item) for item in meta.get("disabled_models", [])}
            prefs = meta.get("model_prefs")
            self.model_prefs = dict(prefs) if isinstance(prefs, dict) else {}
        except Exception:  # noqa: BLE001  元数据读失败不阻断号池
            self.ignored = set()
            self.disabled_models = set()
            self.model_prefs = {}

    def _request(self, method: str, url: str, headers: dict[str, str], body: bytes):
        return self._transport(method, url, headers, body)

    def _persist(self) -> None:
        store.save_accounts_atomic(self._accounts_path(), self.accounts,
                                   ignored=self.ignored,
                                   disabled_models=self.disabled_models,
                                   model_prefs=self.model_prefs)

    # -- 号池管理（前端号池页 RPC 的后端实现）------------------------------

    def rescan(self) -> dict[str, int]:
        """重新扫描凭证：xdpool accounts.json + WorkBuddy 登录态重新入池。"""
        self.discover()
        return {"total": len(self.accounts), "ignored": len(self.ignored),
                "active": len(self.accounts) - sum(
                    1 for a in self.accounts if a.account_id() in self.ignored)}

    def logout(self, account_id: str) -> None:
        """「退出登录」＝移出号池：凭证保留在 accounts.json，可随时恢复。"""
        account_id = str(account_id or "")
        if not account_id:
            raise AccountPoolError("缺少 account_id")
        self.ignored.add(account_id)
        self._persist()

    def restore(self, account_id: str) -> None:
        """从忽略清单恢复并重新扫描入池。"""
        account_id = str(account_id or "")
        if not account_id:
            raise AccountPoolError("缺少 account_id")
        self.ignored.discard(account_id)
        self._persist()
        self.discover()

    def set_model_enabled(self, model_id: str, enabled: bool) -> None:
        """目录开关：禁用的模型不展示、不作为默认。"""
        model_id = str(model_id or "")
        if not model_id:
            raise AccountPoolError("缺少 model_id")
        if enabled:
            self.disabled_models.discard(model_id)
        else:
            self.disabled_models.add(model_id)
        self._persist()

    # -- per-model 偏好（xdpool「上下文/图片/思考」开关移植）---------------

    # 实测拒绝 reasoning_effort:"off" 的模型（off-thinking.ts issue #34，
    # 3.4.4 双网关实测）：声明 canDisableThinking 也对它们隐藏 off——硬墙。
    REFUSES_OFF_MODEL_IDS = {
        "deepseek-v4.1-flash", "deepseek-v4.1-flash-sg", "deepseek-v4-pro",
        "primary-model", "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra",
        "gpt-5.6-luna", "gemini-3.5-flash",
    }
    THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

    def set_model_pref(self, model_id: str, *, enabled: bool | None = None,
                       ctx_budget: int | None = None,
                       image: bool | None = None,
                       thinking: str | None = None) -> dict[str, Any]:
        """更新单模型偏好并持久化；None = 不动该维度。"""
        model_id = str(model_id or "")
        if not model_id:
            raise AccountPoolError("缺少 model_id")
        pref = dict(self.model_prefs.get(model_id) or {})
        if enabled is not None:
            pref["enabled"] = bool(enabled)
            if enabled:
                self.disabled_models.discard(model_id)
            else:
                self.disabled_models.add(model_id)
        if ctx_budget is not None:
            pref["ctx_budget"] = max(0, int(ctx_budget))
        if image is not None:
            pref["image"] = bool(image)
        if thinking is not None:
            thinking = str(thinking)
            if thinking not in ("", *self.THINKING_LEVELS):
                raise AccountPoolError(f"非法思考档位：{thinking}")
            if thinking == "off" and model_id in self.REFUSES_OFF_MODEL_IDS:
                raise AccountPoolError(
                    f"{model_id}: 上游拒绝 reasoning_effort=off（实测硬墙），不提供该档")
            # "" = 清除，回到上游默认
            pref["thinking"] = "" if thinking == "" else thinking
        self.model_prefs[model_id] = pref
        self._persist()
        return dict(pref)

    def _reasoning_effort_for(self, model: str) -> str:
        """思考档位落线值：偏好优先；off 撞硬墙时退回上游默认（不发字段）。"""
        pref = self.model_prefs.get(model) or {}
        effort = str(pref.get("thinking") or "")
        if effort == "off":
            if model in self.REFUSES_OFF_MODEL_IDS:
                return ""
            return "off"
        return effort

    def invoke(self, prompt: str, *, model: str) -> str:
        """模型拉起：一律走账号池 failover（免密 lane 已删除）。"""
        model = str(model or "").strip()
        if not model:
            raise AccountPoolError("缺少 model")
        # 免密 lane 已删除：历史 free:<id> 去掉前缀走号池同名模型，不再调 free_chat。
        if model.startswith("free:"):
            model = model.split(":", 1)[1]
        return self.complete(prompt, model=model)

    # -- 签到（billing 端点，xdpool scheduler 同款）------------------------

    _ALREADY_CHECKIN_MARKERS = ("已签到", "already")
    _NO_OFFER_MARKERS = ("没有可领取", "暂无可领取", "无可领取",
                         "不可领取", "未开启", "没有活动")

    def _billing_base(self, account: WorkBuddyCredential) -> str:
        domain = (account.domain or "").lower()
        if account.region == "global" or domain.endswith(("workbuddy.ai", "codebuddy.ai")):
            if domain.endswith("codebuddy.ai"):
                return "https://www.codebuddy.ai"
            return "https://www.workbuddy.ai"
        return "https://www.codebuddy.cn"

    def _billing_headers(self, account: WorkBuddyCredential) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {account.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if account.uid:
            headers["X-User-Id"] = account.uid
        if account.enterprise_id:
            headers["X-Enterprise-Id"] = account.enterprise_id
            headers["X-Tenant-Id"] = account.enterprise_id
        if account.domain:
            headers["X-Domain"] = account.domain
        return headers

    def _billing_post(self, account: WorkBuddyCredential, path: str) -> dict[str, Any]:
        self._ensure_token(account)
        status, _headers, body = self._request(
            "POST", self._billing_base(account) + path,
            self._billing_headers(account), b"{}")
        try:
            envelope = json.loads(body.decode("utf-8") or "{}")
        except ValueError as exc:
            raise AccountPoolError(f"{account.label()}: 签到响应非 JSON") from exc
        message = str(envelope.get("msg") or envelope.get("message") or "")
        if status != 200 or (envelope.get("code") not in (0, None)):
            raise AccountPoolError(f"{account.label()}: HTTP {status} {message}".strip())
        data = envelope.get("data")
        return data if isinstance(data, dict) else {}

    def checkin(self, account: WorkBuddyCredential) -> dict[str, Any]:
        """单账号一键签到；「已签到」「无可领取」按幂等成功处理。"""
        result: dict[str, Any] = {"label": account.label(), "ok": False}
        try:
            data = self._billing_post(account, "/v2/billing/meter/daily-checkin")
        except AccountPoolError as exc:
            message = str(exc)
            if any(marker in message for marker in self._ALREADY_CHECKIN_MARKERS):
                result.update(ok=True, already=True, message="今日已签到")
            elif any(marker in message for marker in self._NO_OFFER_MARKERS):
                result.update(ok=True, noop=True, message=message or "无可领取")
            else:
                result["error"] = message
            return result
        except Exception as exc:  # noqa: BLE001  网络/协议错误不打断批量签到
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        result.update(ok=True, credit=data.get("credit"),
                      streak_days=data.get("streakDays"),
                      is_streak_day=bool(data.get("isStreakDay")))
        return result

    # -- token ------------------------------------------------------------

    def _ensure_token(self, account: WorkBuddyCredential, *, force: bool = False) -> None:
        now_ms = int(time.time() * 1000)
        if (not force and account.access_token
                and (account.expires_at_ms <= 0
                     or account.expires_at_ms > now_ms + 60_000)):
            return
        if not account.refresh_token:
            raise AccountPoolError(f"{account.label()}: 无 refresh_token，无法刷新")
        base = account.chat_base()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": base + "/",
            "Referer": base + "/",
            "User-Agent": CLIENT_UA,
            "X-Refresh-Token": account.refresh_token,
            "X-Auth-Refresh-Source": "workbuddy",
        }
        if account.enterprise_id:
            headers["X-Enterprise-Id"] = account.enterprise_id
        status, _headers, body = self._request(
            "POST", base + "/v2/plugin/auth/token/refresh", headers, b"")
        if status != 200:
            raise AccountPoolError(
                f"{account.label()}: token 刷新失败 HTTP {status}: {_snippet(body)}")
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except ValueError as exc:
            raise AccountPoolError(f"{account.label()}: 刷新响应非 JSON") from exc
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        access = payload.get("accessToken") or payload.get("access_token")
        if access:
            account.access_token = str(access)
        refresh = payload.get("refreshToken") or payload.get("refresh_token")
        if refresh:
            account.refresh_token = str(refresh)
        expires = payload.get("expiresAtMs") or payload.get("expiresAt")
        if expires:
            account.expires_at_ms = _to_ms(expires)
        refresh_expires = (payload.get("refreshExpiresAtMs")
                           or payload.get("refreshExpiresAt"))
        if refresh_expires:
            account.refresh_expires_at_ms = _to_ms(refresh_expires)
        account.last_refresh_at_ms = now_ms
        self._persist()

    # -- chat -------------------------------------------------------------

    def chat(self, account: WorkBuddyCredential, model: str, messages: list[dict],
             *, conversation_key: str = "") -> str:
        self._ensure_token(account)
        try:
            reply = self._chat_request(account, model, messages, conversation_key)
        except _SessionDead:
            # 401/403：dead_until 已写；尝试刷新一次再重试
            try:
                self._ensure_token(account, force=True)
            except AccountPoolError as refresh_exc:
                account.dead_until = time.time() + self._credential_dead_sec
                raise _SessionDead(str(refresh_exc)) from refresh_exc
            account.dead_until = 0.0  # 刷新成功，解除死亡标记
            reply = self._chat_request(account, model, messages, conversation_key)
        self.served[account.account_id()] = self.served.get(account.account_id(), 0) + 1
        return reply

    def _chat_request(self, account: WorkBuddyCredential, model: str,
                      messages: list[dict], conversation_key: str) -> str:
        base = account.chat_base()
        conv_id, request_id, acp_id = conversation_identity_of(conversation_key)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": base + "/",
            "Referer": base + "/",
            "User-Agent": CLIENT_UA,
            "Content-Type": "application/json",
            "Authorization": f"Bearer {account.access_token}",
            "X-Conversation-ID": conv_id,
            "X-Conversation-Request-ID": request_id,
            "X-Conversation-Message-ID": str(uuid.uuid4()),
            "X-Request-ID": str(uuid.uuid4()),
            "acp-connection-id": acp_id,
            "X-Agent-Intent": "craft",
            "X-Agent-Purpose": "conversation",
            "X-Private-Data": "true",
            "x-codebuddy-request": "1",
        }
        if account.uid:
            headers["X-User-Id"] = account.uid
        else:
            headers["X-No-User-Id"] = "1"
        if account.enterprise_id:
            headers["X-Enterprise-Id"] = account.enterprise_id
        else:
            headers["X-No-Enterprise-Id"] = "1"
        if account.domain:
            headers["X-Domain"] = account.domain
        else:
            headers["X-No-Department-Info"] = "1"
        # 上游硬门（实测 11101）：Non-stream chat request is currently not
        # supported —— 必须流式；SSE 解析器本就兼容（transport 读完整 body）。
        payload = json.dumps({"model": model, "messages": messages,
                              "stream": True}).encode("utf-8")
        # 思考档位落线（xdpool off-thinking/default-effort 移植）：
        # 声明过的档位随体注入；off 撞硬墙（REFUSES_OFF_MODEL_IDS）已在上游剔除。
        effort = self._reasoning_effort_for(model)
        if effort:
            body = json.loads(payload.decode("utf-8"))
            body["reasoning_effort"] = effort
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        status, resp_headers, body = self._request(
            "POST", base + "/v2/chat/completions", headers, payload)

        # ★ 业务码优先于 HTTP 状态（照抄 wbmod classifyUpstreamError 顺序，issue #56/#127）。
        # 响应体可能带 business code，也可能只有文案标记；两者都覆盖，先 wallet 后 throttle。
        lower = body.decode("utf-8", "replace").lower()
        code = _business_code(body)
        # 1) 欠费：额度耗尽，换号可救，本号当日别再试
        if status == 402 or code == _CREDITS_EXHAUSTED_CODE \
                or any(marker in lower for marker in _HARD_CREDIT_MARKERS):
            raise _OutOfCredit(f"{account.label()}: 额度已用尽（code={code}），换号重试")
        # 2) 登录态失效：刷新重试 or 换号；文案标记覆盖非 401/403 的伪装
        if status in (401, 403) or "12153" in lower \
                or any(marker in lower for marker in _SESSION_DEAD_MARKERS):
            raise _SessionDead(f"{account.label()}: 登录态失效（HTTP {status}/code={code}）")
        # 3) 模型忙（14003）：全局模型负载，**只冷该模型 2 分钟，绝不冷账号**
        #    一个模型抖动不能把整池每个账号都冷 30 分钟（issue #56）。
        if code == _MODEL_BUSY_CODE or "model is busy" in lower or "模型繁忙" in lower \
                or "负载较高" in lower:
            account.cooldowns[model] = time.time() + _MODEL_BUSY_COOLDOWN_SEC
            raise _RateLimited(
                f"{account.label()}: 模型 {model} 繁忙，仅冷模型 {_MODEL_BUSY_COOLDOWN_SEC:.0f}s")
        # 4) 账号级限流（6004 / 其它 429）：恢复时间优先读响应体文案，再退响应头
        if status == 429 or code == _RATE_LIMITED_CODE:
            reset_at = _reset_at_ms(body)
            retry_after = (reset_at - time.time()) if reset_at else 0.0
            if retry_after <= 0:
                raw = _header(resp_headers, "Retry-After")
                try:
                    retry_after = float(raw) if raw else 60.0
                except ValueError:
                    retry_after = 60.0
            account.cooldowns[model] = time.time() + retry_after
            raise _RateLimited(
                f"{account.label()}: 账号限流（code={code}），冷 {model} {retry_after:.0f}s")
        if status != 200:
            raise AccountPoolError(
                f"{account.label()}: chat 上游 HTTP {status}: {_snippet(body)}")
        return _parse_chat_payload(body, resp_headers)

    # -- failover ---------------------------------------------------------

    def complete(self, prompt: str, *, model: str | None = None,
                 conversation_key: str = "",
                 images: list[str] | None = None) -> str:
        model = model or self._default_model
        content: Any = prompt
        if images:
            # 多模态：OpenAI 兼容 content 数组（text + image_url）；
            # data URL / http(s) URL 直接透传上游 /v2/chat/completions。
            parts: list[dict] = []
            if str(prompt or "").strip():
                parts.append({"type": "text", "text": str(prompt)})
            for src in images:
                parts.append({"type": "image_url", "image_url": {"url": str(src)}})
            content = parts or prompt
        messages = [{"role": "user", "content": content}]
        now = time.time()
        eligible = [a for a in self.accounts
                    if a.account_id() not in self.ignored
                    and now >= a.dead_until
                    and now >= a.cooldowns.get(model, 0.0)]
        if not eligible:
            raise AccountPoolError("号池没有可用账号（未 discover 或全部冷却/死亡）")
        last: Exception | None = None
        for account in eligible[: self._max_attempts]:
            try:
                return self.chat(account, model, messages,
                                 conversation_key=conversation_key)
            # 可换号的软故障：账号限流 / 模型忙 / 登录失效 / 额度耗尽。
            # 不可换号的硬故障（上游协议错、网络错）直接抛，换号也救不了。
            except (_RateLimited, _SessionDead, _OutOfCredit) as exc:
                last = exc
                continue
        raise AccountPoolError(f"号池全部 {self._max_attempts} 次尝试失败: {last}")

    # -- 免密 lane ----------------------------------------------------------

    def free_chat(self, prompt: str, *, model: str) -> str:
        if not self.free_lane_enabled:
            raise AccountPoolError("免密 lane 未开启（.env POOL_FREE_LANE=true 后重启后端）")
        if model.startswith("muse-spark"):
            path, payload = "/inference/v1/responses", {
                "model": model, "input": prompt, "stream": True}
        elif model.startswith("union-alpha"):
            path, payload = "/inference/v1/messages", {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 4096, "stream": True}
        else:
            path, payload = "/inference/v1/chat/completions", {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True}
        session = mint_session_id()
        headers = {
            "accept": "text/event-stream",
            "authorization": "Bearer public",
            "user-agent": FREE_CLIENT_UA,
            "x-opencode-client": "cli",
            "x-opencode-project": "global",
        }
        for name in _FREE_SESSION_HEADERS:  # 四连同值；绝不带 x-opencode-request
            headers[name] = session
        status, resp_headers, body = self._request(
            "POST", FREE_BASE + path, headers, json.dumps(payload).encode("utf-8"))
        if status != 200:
            snippet = _snippet(body)
            if status == 429 and "FreeUsageLimit" in snippet:
                raise AccountPoolError(
                    "免费额度已用尽（OpenCode 免费模型有使用限额）——"
                    "明天再试，或从模型菜单换 WorkBuddy 模型")
            raise AccountPoolError(f"免密 lane HTTP {status}: {snippet}")
        return _parse_chat_payload(body, resp_headers)


class PoolClient:
    """与 gateway.model_router.ModelClient 兼容的号池客户端。"""

    def __init__(self, pool: AccountPool, config: Any) -> None:
        self.pool = pool
        self.config = config
        self.name = "account-pool"
        self.kind = "cloud"
        self.caps = ("chat", "tools", "json_mode")
        self.weight = 1.0
        self.successes = 0
        self.failures = 0
        self.latency_ms = 100.0

    @property
    def success_rate(self) -> float:
        total = self.successes + self.failures
        return (self.successes + 1.0) / (total + 1.0)

    def record(self, ok: bool, latency_ms: float) -> None:
        if ok:
            self.successes += 1
        else:
            self.failures += 1
        self.latency_ms = 0.7 * self.latency_ms + 0.3 * max(1.0, latency_ms)

    def complete(self, prompt: str, model: str | None = None,
                 *, images: list[str] | None = None) -> str:
        return self.pool.complete(prompt, model=model or self.pool._default_model,
                                  images=images)
