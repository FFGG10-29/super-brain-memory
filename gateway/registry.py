"""gateway.registry —— Agent 注册中心（动态接入 / 令牌签发 / 命名空间策略）。

三步接入：申请令牌 → 分配 agent_id 与命名空间作用域 → 发放接入串。
安全：
- 令牌只存 SHA-256 哈希，明文只在签发时返回一次；
- 签名密钥走 `GW_MCP_TOKEN_SECRET`（env），缺失时用进程内临时密钥
  （重启后旧令牌失效 —— 生产必须配置，否则会被强制轮换）；
- agent_id 由服务端签发，客户端自报无效；
- 每次调用写 access_log（DuckDB，只写不改）。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from config import BrainConfig, get_config
from core.errors import GatewayAuthError
from core.interfaces import KVRepository, TimeSeriesRepository
from core.ids import new_uuid7

__all__ = ["Agent", "AgentRegistry"]

_BUCKET = "agents"
_POLICY_BUCKET = "policy"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Agent:
    agent_id: str
    name: str
    scopes: tuple[str, ...]
    permissions: tuple[str, ...]
    token_hash: str
    created_at_ms: int
    revoked: bool = False

    def to_json(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "scopes": list(self.scopes),
            "permissions": list(self.permissions),
            "token_hash": self.token_hash,
            "created_at_ms": self.created_at_ms,
            "revoked": self.revoked,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Agent":
        return cls(
            agent_id=str(data["agent_id"]),
            name=str(data["name"]),
            scopes=tuple(data.get("scopes") or ("private",)),
            permissions=tuple(data.get("permissions") or ("read",)),
            token_hash=str(data["token_hash"]),
            created_at_ms=int(data.get("created_at_ms", 0)),
            revoked=bool(data.get("revoked", False)),
        )


class AgentRegistry:
    def __init__(
        self,
        kv: KVRepository,
        *,
        timeseries: TimeSeriesRepository | None = None,
        config: BrainConfig | None = None,
    ) -> None:
        self._kv = kv
        self._timeseries = timeseries
        self._config = config or get_config()
        configured = self._config.gateway.mcp_token_secret
        self._secret = configured or secrets.token_hex(32)
        self.ephemeral_secret = not bool(configured)

    def register(
        self,
        name: str,
        *,
        scopes: tuple[str, ...] = ("private",),
        permissions: tuple[str, ...] = ("read", "write"),
    ) -> tuple[str, str]:
        """返回 (agent_id, 一次性明文令牌)。"""
        if not name.strip():
            raise GatewayAuthError("Agent 名称不能为空")
        valid_scopes = ("private", "group", "global")
        if any(scope not in valid_scopes for scope in scopes):
            raise GatewayAuthError(f"非法命名空间: {scopes}")
        agent_id = f"agent-{new_uuid7()}"
        nonce = secrets.token_hex(16)
        signature = hmac.new(
            self._secret.encode("utf-8"), f"{agent_id}.{nonce}".encode("utf-8"), hashlib.sha256
        ).hexdigest()[:32]
        token = f"{agent_id}.{nonce}.{signature}"
        agent = Agent(
            agent_id=agent_id,
            name=name.strip(),
            scopes=tuple(scopes),
            permissions=tuple(permissions),
            token_hash=_digest(token),
            created_at_ms=_now_ms(),
        )
        self._kv.put_json(_BUCKET, agent_id, agent.to_json())
        return agent_id, token

    def verify(self, token: str | None) -> Agent:
        if not token or token.count(".") != 2:
            raise GatewayAuthError("令牌格式无效")
        agent_id, nonce, signature = token.split(".")
        expected = hmac.new(
            self._secret.encode("utf-8"), f"{agent_id}.{nonce}".encode("utf-8"), hashlib.sha256
        ).hexdigest()[:32]
        if not hmac.compare_digest(signature, expected):
            raise GatewayAuthError("令牌签名无效")
        raw = self._kv.get_json(_BUCKET, agent_id)
        if not raw:
            raise GatewayAuthError("Agent 未注册")
        agent = Agent.from_json(raw)
        if agent.revoked or not hmac.compare_digest(agent.token_hash, _digest(token)):
            raise GatewayAuthError("令牌已吊销或不匹配")
        return agent

    def revoke(self, agent_id: str) -> None:
        raw = self._kv.get_json(_BUCKET, agent_id)
        if not raw:
            raise GatewayAuthError("Agent 未注册")
        agent = Agent.from_json(raw)
        self._kv.put_json(_BUCKET, agent_id, Agent(**{**agent.to_json(), "revoked": True}).to_json())

    @staticmethod
    def namespace_allowed(agent: Agent, namespace: str) -> bool:
        return namespace in agent.scopes

    @staticmethod
    def permission_allowed(agent: Agent, permission: str) -> bool:
        return permission in agent.permissions

    def audit(self, agent_id: str, action: str, *, ok: bool = True, latency_ms: float = 0.0) -> None:
        if self._timeseries is None:
            return
        self._timeseries.append(
            "access_log",
            [
                {
                    "ts_ms": _now_ms(),
                    "agent_id": agent_id,
                    "channel": action,
                    "query": action,
                    "hits": 1 if ok else 0,
                    "latency_ms": float(latency_ms),
                    "ok": bool(ok),
                }
            ],
        )

    # ── POLICY.json 同步（随事件总线跨设备）────────────────────────────
    def export_policy(self) -> dict:
        entries = self._kv.scan_prefix(_POLICY_BUCKET, "", limit=1000)
        return {key: value.decode("utf-8") for key, value in entries}

    def import_policy(self, policy: dict) -> None:
        for key, value in policy.items():
            self._kv.put(_POLICY_BUCKET, key, str(value).encode("utf-8"))
