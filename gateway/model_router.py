"""gateway.model_router —— 进程内模型路由函数 + 供应商客户端池（v4.1 W3 / P3）。

不是 RPC 服务：`route(intent, session_id, required_caps) -> ModelClient`。
裁决依据：B1/Q15/R3（默认关闭云模型，路由层始终可用）、C8/R9（配置变更回调热重载）、
P3.2（粘性 / intent 突破 / 加权轮询 / fallback / required_caps / 隐私过滤）、
B2（API Key 走 SecretStore）、Q20（intent 与召回深度同源，由 ChatEngine 产出）。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from config import BrainConfig, get_config
from core.cache_manager import get_cache_manager
from core.user_config import on_change

__all__ = ["ModelClient", "ModelRouter", "get_model_router", "reset_model_router"]

LOCAL = "local"
CLOUD = "cloud"
PAID = "paid"

# C16（Q34）：只有确定性问答 intent 才写输出缓存；emotion/consolidation/media 不写
_OUTPUT_CACHE_INTENTS = frozenset({"chat", "code", "tool_call"})


def _output_cache_key(prompt: str, preference: str, intent: str) -> str:
    """C16 键：sha256(normalize(prompt) + model偏好 + intent)，一律 str。
    normalize = 去首尾空白 + 压缩连续空白 + lower，语义等价输入共享同一键。"""
    normalized = " ".join(str(prompt).split()).lower()
    return hashlib.sha256(
        (normalized + "\x00" + str(preference) + "\x00" + str(intent))
        .encode("utf-8")).hexdigest()


@dataclass
class ModelClient:
    """供应商客户端。测试可注入子类覆盖 complete()，不发真实网络请求。"""

    name: str
    kind: str = LOCAL
    endpoint: str = ""
    model: str = ""
    caps: tuple[str, ...] = ("chat",)
    weight: float = 1.0
    api_key: str = ""
    timeout_sec: float = 20.0
    successes: int = 0
    failures: int = 0
    latency_ms: float = 100.0
    headers: dict[str, str] = field(default_factory=dict)

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

    def complete(self, prompt: str) -> str:
        """Ollama / OpenAI 兼容两种协议；注入 fake client 时不走这里。"""
        if not self.endpoint:
            raise RuntimeError(f"{self.name}: 未配置 endpoint")
        if self.kind == LOCAL:
            url = self.endpoint.rstrip("/") + "/api/generate"
            payload: dict[str, Any] = {"model": self.model, "prompt": prompt, "stream": False}
        else:
            url = self.endpoint.rstrip("/") + "/chat/completions"
            payload = {"model": self.model,
                       "messages": [{"role": "user", "content": prompt}]}
        headers = {"Content-Type": "application/json", **self.headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
            data = json.loads(response.read().decode("utf-8"))
        if self.kind == LOCAL:
            return str(data.get("response") or "")
        return str(data["choices"][0]["message"]["content"])


class LocalGGUFClient(ModelClient):
    """Q15 / R3：Ollama 不可达时退本地 0.6B GGUF（不联网、不出本机）。"""

    def __init__(self, llm: Any, *, timeout_sec: float = 20.0) -> None:
        super().__init__(name="local-gguf", kind=LOCAL,
                         caps=("chat", "tools", "json_mode"),
                         weight=0.5, timeout_sec=timeout_sec)
        self._llm = llm

    def complete(self, prompt: str) -> str:
        text = self._llm.complete_text(
            "你是本地记忆助手，只依据给定上下文简洁作答，不要编造。", prompt)
        if not text.strip():
            raise RuntimeError("GGUF 返回空文本")
        return text


class ModelRouter:
    def __init__(
        self,
        config: BrainConfig | None = None,
        *,
        secret_store: Any | None = None,
        clients: list[ModelClient] | None = None,
        on_fallback: Callable[[dict[str, Any]], None] | None = None,
        local_llm: Any | None = None,
    ) -> None:
        self._config = config or get_config()
        self._secret_store = secret_store
        self._local_llm = local_llm
        self._injected = clients
        self._on_fallback = on_fallback
        self._pool: list[ModelClient] = []
        # §16.14 号池实例（非 ModelClient 列表）：避免与供应商列表 self._pool 撞名
        self._account_pool: Any | None = None
        # C07（Q29）：session_id -> (client_name, intent, expires_at,
        # required_caps, fallback_chain)。同 (intent, caps) 粘性生效；intent 或
        # caps 变化允许突破；fallback_chain 由 complete() 构造 ordered 顺序后写回，
        # 下次同 session 同 (intent, caps) 直接复用（链上仍逐个重跑 _eligible）。
        self._sticky: dict[str, tuple[str, str, float, tuple[str, ...], list[str]]] = {}
        self.fallback_events: list[dict[str, Any]] = []
        self.reload()
        on_change(self._on_config_change)

    # ── 配置热重载（C8）───────────────────────────────────────────────────
    def _on_config_change(self, patches: dict[str, Any]) -> None:
        if "model_router" in patches:
            self.reload()

    def reload(self) -> None:
        """重建供应商池与 intent_map；正在进行的 session 靠 sticky 继续用旧模型。"""
        if self._injected is not None:
            self._pool = list(self._injected)
            return
        cfg = self._config.model_router
        self._pool = [ModelClient(
            name="local-ollama", kind=LOCAL, endpoint=cfg.local_endpoint,
            model=cfg.local_model, caps=("chat", "tools", "json_mode"),
            timeout_sec=float(cfg.request_timeout_sec),
        )]
        # Q15：Ollama 不可达时再退本地 0.6B GGUF（complete() 按 pool 顺序 fallback）
        if self._local_llm is not None and hasattr(self._local_llm, "complete_text"):
            self._pool.append(LocalGGUFClient(
                self._local_llm, timeout_sec=float(cfg.request_timeout_sec)))
        # §16.14 号池网关：enabled 时在本地 GGUF 之后追加号池 provider
        #（PoolClient.complete() 走号池内部 lane 而非 HTTP）。模块缺失/初始化失败
        # 一律忽略，不进候选池（号池是可选能力）。
        self._account_pool = None
        if self._config.account_pool.enabled:
            try:
                from gateway.account_pool.pool import AccountPool, PoolClient
                pool = AccountPool(self._config)
                pool.discover()
                if pool.accounts:
                    self._account_pool = pool
                    self._pool.append(PoolClient(pool, self._config))
            except Exception:  # noqa: BLE001  号池不可用不影响路由层
                self._account_pool = None
        if not cfg.enabled:
            return  # B1/Q15：默认不把云供应商放进候选池
        for item in cfg.providers or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            name = str(item["name"])
            key = ""
            if self._secret_store is not None:
                key = self._secret_store.get(f"model_router.{name}")
            self._pool.append(ModelClient(
                name=name,
                kind=str(item.get("kind") or CLOUD),
                endpoint=str(item.get("endpoint") or ""),
                model=str(item.get("model") or ""),
                caps=tuple(str(cap) for cap in (item.get("caps") or ("chat",))),
                weight=float(item.get("weight") or 1.0),
                api_key=key or str(item.get("api_key") or ""),
                timeout_sec=float(cfg.request_timeout_sec),
            ))

    # ── 路由 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _eligible(client: ModelClient, required_caps: tuple[str, ...],
                  sensitive: bool) -> bool:
        if any(cap not in client.caps for cap in required_caps):
            return False
        # P3.4 / B4：敏感记忆只能走本地 Ollama 或付费 API，绝不发给免费云模型
        if sensitive and client.kind == CLOUD:
            return False
        return True

    def route(
        self,
        intent: str,
        session_id: str,
        required_caps: tuple[str, ...] | list[str] = (),
        *,
        sensitive: bool = False,
    ) -> ModelClient:
        caps = tuple(str(cap) for cap in (required_caps or ()))
        now = time.time()
        sticky = self._sticky.get(session_id)
        # C07：粘性命中要求 intent 与 caps 都一致；_eligible 仍要重跑（健康可变）
        if (sticky and sticky[1] == intent and sticky[2] > now
                and sticky[3] == caps):
            for client in self._pool:
                if client.name == sticky[0] and self._eligible(client, caps, sensitive):
                    return client
        candidates = [c for c in self._pool if self._eligible(c, caps, sensitive)]
        if not candidates:
            # 没有合格模型时退回本地（仍可能失败，但绝不把敏感内容发上云）
            candidates = [c for c in self._pool if c.kind == LOCAL] or self._pool
        # intent → 模型档位：intent_map 给出能力标签（fast/strong/persona/tools），
        # 命中标签的候选加权；同能力供应商之间再按 权重×成功率×延迟倒数 做加权轮询。
        preference = str(self._config.model_router.intent_map.get(intent, ""))

        def score(client: ModelClient) -> float:
            boost = 1.5 if preference and preference in client.caps else 1.0
            return (client.weight * client.success_rate
                    / (1.0 + client.latency_ms / 1000.0) * boost)

        best = max(candidates, key=score)
        ttl = float(self._config.model_router.sticky_ttl_sec)
        # C07：同 (intent, caps) 未过期时保留旧 fallback_chain，否则清空待 complete() 写回
        chain: list[str] = []
        if sticky and sticky[1] == intent and sticky[2] > now and sticky[3] == caps:
            chain = list(sticky[4])
        self._sticky[session_id] = (best.name, intent, now + ttl, caps, chain)
        return best

    def complete(
        self,
        prompt: str,
        *,
        intent: str,
        session_id: str,
        required_caps: tuple[str, ...] | list[str] = (),
        sensitive: bool = False,
        memory_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """调用并按 providers 顺序 fallback；429/超时/异常都记事件，不抛给 ChatEngine。

        C16：output 缓存开启且 intent ∈ {chat, code, tool_call} 时入口先查精确输出
        缓存（sensitive 一律不读不写，防敏感内容落盘）；命中零模型调用直接返回。
        `memory_ids` 由 ChatEngine 接线传入，写进缓存值的 affected_ids，
        使 `memory_updated` 总线失效可精确命中（C0 按值匹配）。
        """
        caps = tuple(str(cap) for cap in (required_caps or ()))
        mgr = get_cache_manager()
        cache_key = ""
        if mgr is not None and not sensitive and intent in _OUTPUT_CACHE_INTENTS:
            cache_key = _output_cache_key(
                prompt, str(self._config.model_router.intent_map.get(intent, "")), intent)
            cached = mgr.get("output", cache_key)
            if isinstance(cached, dict) and cached.get("text"):
                return {"ok": True, "text": str(cached["text"]),
                        "model": str(cached.get("model") or ""), "kind": "cache",
                        "fallback": False, "cache_hit": True}

        first = self.route(intent, session_id, caps, sensitive=sensitive)
        # C07：sticky 已缓存同 (intent, caps) 的 fallback 链就直接复用；
        # 链上每个客户端仍要重跑 _eligible（供应商健康/能力可能变化）。
        now = time.time()
        sticky = self._sticky.get(session_id)
        same_key = bool(sticky and sticky[1] == intent and sticky[3] == caps
                        and sticky[2] > now)
        ordered: list[ModelClient] = []
        if same_key and sticky[4]:
            by_name = {c.name: c for c in self._pool}
            ordered = [by_name[name] for name in sticky[4]
                       if name in by_name
                       and self._eligible(by_name[name], caps, sensitive)]
        if not ordered:
            ordered = [first] + [c for c in self._pool if c is not first
                                 and self._eligible(c, caps, sensitive)]
        # C07：把本次 ordered 顺序写回 sticky，下次同 (intent, caps) 直接复用；
        # 未过期的同键 sticky 保留原 expires_at，避免粘性被 frequent complete 无限续期。
        expires = sticky[2] if same_key else now + float(
            self._config.model_router.sticky_ttl_sec)
        self._sticky[session_id] = (first.name, intent, expires, caps,
                                    [c.name for c in ordered])
        for client in ordered:
            started = time.perf_counter()
            try:
                text = client.complete(prompt)
                if text:
                    client.record(True, (time.perf_counter() - started) * 1000)
                    if cache_key:
                        # C16：只缓存确定性问答 intent；值含 affected_ids 供精确失效
                        mgr.set("output", cache_key,
                                {"text": text, "model": client.name,
                                 "affected_ids": list(memory_ids or [])})
                    return {"ok": True, "text": text, "model": client.name,
                            "kind": client.kind, "fallback": client is not first,
                            "cache_hit": False}
                raise RuntimeError("空回复")
            except Exception as exc:  # noqa: BLE001  任一供应商失败都不中断对话
                client.record(False, (time.perf_counter() - started) * 1000)
                event = {"session_id": session_id, "intent": intent,
                         "model": client.name, "error": type(exc).__name__,
                         "ts_ms": int(time.time() * 1000)}
                self.fallback_events.append(event)
                if self._on_fallback is not None:
                    self._on_fallback(event)
        return {"ok": False, "text": "暂时无法生成回复（模型服务不可用）",
                "model": "", "kind": LOCAL, "fallback": True, "cache_hit": False}


_router: ModelRouter | None = None


def get_model_router(config: BrainConfig | None = None, **kwargs: Any) -> ModelRouter:
    global _router
    if _router is None:
        _router = ModelRouter(config, **kwargs)
    return _router


def reset_model_router() -> None:
    global _router
    _router = None
