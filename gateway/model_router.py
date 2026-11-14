"""gateway.model_router —— 进程内模型路由函数 + 供应商客户端池（v4.1 W3 / P3）。

不是 RPC 服务：`route(intent, session_id, required_caps) -> ModelClient`。
裁决依据：B1/Q15/R3（默认关闭云模型，路由层始终可用）、C8/R9（配置变更回调热重载）、
P3.2（粘性 / intent 突破 / 加权轮询 / fallback / required_caps / 隐私过滤）、
B2（API Key 走 SecretStore）、Q20（intent 与召回深度同源，由 ChatEngine 产出）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
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

# C17：时效性 prompt（答案随时间/状态变化）强制绕过语义缓存的查与写
_SEMANTIC_BYPASS_RE = re.compile(r"今天|现在|最近|几点|\d{4}年")


def _normalize(text: str) -> str:
    """C16/C17 共用：去首尾空白 + 压缩连续空白 + lower。"""
    return " ".join(str(text).split()).lower()


def _output_cache_key(prompt: str, preference: str, intent: str) -> str:
    """C16 键：sha256(normalize(prompt) + model偏好 + intent)，一律 str。"""
    return hashlib.sha256(
        (_normalize(prompt) + "\x00" + str(preference) + "\x00" + str(intent))
        .encode("utf-8")).hexdigest()


def _semantic_key(prompt: str) -> str:
    """C17 键：sha256(normalize(prompt))（不含偏好/intent，语义等价输入共享条目）。"""
    return hashlib.sha256(_normalize(prompt).encode("utf-8")).hexdigest()


def _cosine(a: Any, b: Any) -> float:
    """稠密向量余弦；非法/零向量一律 0.0（宁可 miss 不可假命中）。"""
    try:
        num = sum(float(x) * float(y) for x, y in zip(a, b))
        da = math.sqrt(sum(float(x) ** 2 for x in a))
        db = math.sqrt(sum(float(y) ** 2 for y in b))
    except (TypeError, ValueError):
        return 0.0
    if da <= 0.0 or db <= 0.0:
        return 0.0
    return num / (da * db)


def _multimodal_content(prompt: str, images: list[str] | None) -> Any:
    """构造 OpenAI 兼容 user content：无图返回纯字符串；有图返回 text+image_url 数组。

    images 元素是 data URL（data:image/...;base64,...）或 http(s) URL，直接透传上游。
    """
    if not images:
        return prompt
    parts: list[dict[str, Any]] = []
    if str(prompt or "").strip():
        parts.append({"type": "text", "text": str(prompt)})
    for src in images:
        parts.append({"type": "image_url", "image_url": {"url": str(src)}})
    return parts or prompt


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

    def complete(self, prompt: str, *, images: list[str] | None = None) -> str:
        """Ollama / OpenAI 兼容两种协议；注入 fake client 时不走这里。

        images 非空时走 OpenAI 多模态 content 数组（仅 cloud 协议支持；本地协议忽略）。
        """
        if not self.endpoint:
            raise RuntimeError(f"{self.name}: 未配置 endpoint")
        if self.kind == LOCAL:
            url = self.endpoint.rstrip("/") + "/api/generate"
            payload: dict[str, Any] = {"model": self.model, "prompt": prompt, "stream": False}
        else:
            url = self.endpoint.rstrip("/") + "/chat/completions"
            payload = {"model": self.model,
                       "messages": [{"role": "user",
                                     "content": _multimodal_content(prompt, images)}]}
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

    def complete(self, prompt: str, *, images: list[str] | None = None) -> str:
        text = self._llm.complete_text(
            "你是本地记忆助手，只依据给定上下文简洁作答，不要编造。", prompt)
        if not text.strip():
            raise RuntimeError("GGUF 返回空文本")
        return text


logger = logging.getLogger("model_router")


class ModelRouter:
    def __init__(
        self,
        config: BrainConfig | None = None,
        *,
        secret_store: Any | None = None,
        clients: list[ModelClient] | None = None,
        on_fallback: Callable[[dict[str, Any]], None] | None = None,
        local_llm: Any | None = None,
        embedder: Any | None = None,
    ) -> None:
        self._config = config or get_config()
        self._secret_store = secret_store
        self._local_llm = local_llm
        self._embedder = embedder
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

    @staticmethod
    def _providers_overlay() -> list[dict[str, Any]]:
        """前端"模型配置"页写入的供应商覆盖（user_dir/model_providers.json）。"""
        import json

        from core.runtime_paths import user_dir

        path = user_dir() / "model_providers.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    @staticmethod
    def save_providers_overlay(providers: list[dict[str, Any]]) -> None:
        import json

        from core.runtime_paths import user_dir

        path = user_dir() / "model_providers.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(providers, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    @staticmethod
    def _task_routes_overlay() -> dict[str, str]:
        """内部消费者路由覆盖（user_dir/task_routes.json，优先于 config）。"""
        import json

        from core.runtime_paths import user_dir

        path = user_dir() / "task_routes.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def save_task_routes_overlay(routes: dict[str, str]) -> None:
        import json

        from core.runtime_paths import user_dir

        path = user_dir() / "task_routes.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(routes, ensure_ascii=False, indent=2),
                        encoding="utf-8")
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
                    # 号池显式开启且有账号 → 提到候选池首位作默认模型源。
                    # 理由：用户显式开 POOL_ENABLED 就是要用多账号/白嫖额度；本地
                    # Ollama 未安装时排列首位只会每次空等超时（实测 8769 聊天因
                    # local-ollama 优先而整链失败）。隐私底线不破：sensitive=True
                    # 时 _eligible 排除 kind=cloud 的号池，敏感内容仍走本地；号池
                    # 调用失败时 complete() 按 ordered 链照常 fallback 到本地。
                    self._pool.insert(0, PoolClient(pool, self._config))
            except Exception:  # noqa: BLE001  号池不可用不影响路由层
                self._account_pool = None
        if not cfg.enabled:
            return  # B1/Q15：默认不把云供应商放进候选池
        providers = self._providers_overlay() or list(cfg.providers or [])
        for item in providers:
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
        # v4.1-C 全开：路由决策镜像写 ``route`` ns（键=session_id，值含 client/intent/caps/
        # 到期时间的快照）。_sticky 仍是权威（同进程TTL语义）；route ns 仅作可观测 +
        # 跨调用诊断（"这轮为何选它"），未装配/异常静默不影响路由。
        try:
            mgr = get_cache_manager()
            if mgr is not None:
                mgr.set("route", str(session_id), {
                    "client": best.name, "intent": intent, "caps": list(caps),
                    "expires_at": now + ttl, "chain": list(chain),
                }, ttl_sec=ttl)
        except Exception:  # noqa: BLE001  路由缓存失败不阻断选型
            pass
        return best

    # ── C17：语义输出缓存 ─────────────────────────────────────────────────
    def _semantic_embedding(self, prompt: str, intent: str) -> list[float] | None:
        """可做语义缓存时返回 prompt 的 embedding；跳过条件（配置关/无 embedder/
        intent 不可缓存/时效性 prompt）返回 None。异常一律回退 None 不影响主链路。"""
        if (not self._config.model_router.semantic_cache
                or self._embedder is None
                or intent not in _OUTPUT_CACHE_INTENTS
                or _SEMANTIC_BYPASS_RE.search(str(prompt))):
            return None
        mgr = get_cache_manager()
        if mgr is None:
            return None
        try:
            vector = self._embedder.embed_query(_normalize(prompt))
            return [float(x) for x in (vector or [])]
        except Exception:  # noqa: BLE001  embedder 故障不阻断模型调用
            return None

    def _semantic_lookup(self, embedding: list[float], intent: str) -> dict[str, Any] | None:
        """线性扫 semantic ns 按 intent 阈值取近邻；阈值缺省取最保守 0.95。"""
        if not embedding:
            return None
        mgr = get_cache_manager()
        if mgr is None:
            return None
        threshold = float(self._config.model_router.semantic_thresholds.get(intent, 0.95))
        try:
            entries = mgr.iter_entries("semantic")
        except Exception:  # noqa: BLE001
            return None
        for _key, value in entries:
            if not isinstance(value, dict) or not value.get("text"):
                continue
            if _cosine(embedding, value.get("embedding") or []) >= threshold:
                return value
        return None

    def _semantic_store(self, prompt: str, embedding: list[float], text: str,
                        model: str, memory_ids: list[str] | None) -> None:
        mgr = get_cache_manager()
        if mgr is None or not embedding:
            return
        try:
            mgr.set("semantic", _semantic_key(prompt),
                    {"embedding": embedding, "text": text, "model": model,
                     "affected_ids": list(memory_ids or [])})
        except Exception:  # noqa: BLE001  写缓存失败不影响对话
            pass

    def complete(
        self,
        prompt: str,
        *,
        intent: str,
        session_id: str,
        required_caps: tuple[str, ...] | list[str] = (),
        sensitive: bool = False,
        memory_ids: list[str] | None = None,
        model: str = "",
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        """调用并按 providers 顺序 fallback；429/超时/异常都记事件，不抛给 ChatEngine。

        C16：output 缓存开启且 intent ∈ {chat, code, tool_call} 时入口先查精确输出
        缓存（sensitive 一律不读不写，防敏感内容落盘）；命中零模型调用直接返回。
        `memory_ids` 由 ChatEngine 接线传入，写进缓存值的 affected_ids，
        使 `memory_updated` 总线失效可精确命中（C0 按值匹配）。
        C17：精确 miss 且语义可缓存时做 embedding 近邻查找（cache_kind="semantic"）。
        `images` 非空（多模态聊天）：输出/语义缓存一律跳过（键只有 prompt，
        同文异图会假命中），并把图片随 OpenAI content 数组透传给模型。
        """
        caps = tuple(str(cap) for cap in (required_caps or ()))
        mgr = get_cache_manager()
        cache_key = ""
        if (mgr is not None and not sensitive and not images
                and intent in _OUTPUT_CACHE_INTENTS):
            cache_key = _output_cache_key(
                prompt, str(self._config.model_router.intent_map.get(intent, "")), intent)
            cached = mgr.get("output", cache_key)
            if isinstance(cached, dict) and cached.get("text"):
                return {"ok": True, "text": str(cached["text"]),
                        "model": str(cached.get("model") or ""), "kind": "cache",
                        "fallback": False, "cache_hit": True}

        # C17：精确缓存 miss 后做语义近邻查找；sensitive/带图一律不做语义读/写。
        # embedding 保存在 sem_embedding，成功后写入复用，避免二次编码。
        sem_embedding = (None if sensitive or images
                         else self._semantic_embedding(prompt, intent))
        if sem_embedding:
            hit = self._semantic_lookup(sem_embedding, intent)
            if hit is not None:
                return {"ok": True, "text": str(hit["text"]),
                        "model": str(hit.get("model") or ""), "kind": "cache",
                        "fallback": False, "cache_hit": True,
                        "cache_kind": "semantic"}

        first = self.route(intent, session_id, caps, sensitive=sensitive)
        # 用户显式指定模型（聊天页模型选择）：定向调用，不走 intent 路由链。
        # free:* / 免密 id → 免密 lane；其余 → 号池 failover。失败如实报错，
        # 不静默换成别的模型（用户点名的是这一个）。
        if model:
            return self._complete_explicit(prompt, model, session_id, images=images)
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
                # 带图才传 images kwarg（fake client/旧签名兼容：无图保持 complete(prompt)）
                text = (client.complete(prompt, images=images) if images
                        else client.complete(prompt))
                if text:
                    client.record(True, (time.perf_counter() - started) * 1000)
                    if cache_key:
                        # C16：只缓存确定性问答 intent；值含 affected_ids 供精确失效
                        mgr.set("output", cache_key,
                                {"text": text, "model": client.name,
                                 "affected_ids": list(memory_ids or [])})
                    if sem_embedding:
                        # C17：写 semantic ns，值含 affected_ids 供 memory_updated 失效
                        self._semantic_store(prompt, sem_embedding, text,
                                             client.name, memory_ids)
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

    def complete_task(self, task: str, prompt: str) -> str:
        """内部消费者（抽取/精排/巩固）按 task_routes 定向调用远程供应商。

        spec："pool:<model>" 走号池 failover；"provider:<name>" 走配置的云供应商；
        "cloud:<pid>|<model>" 走注册表供应商的具体模型（key 供应商级共用，
        同一供应商不同用途可各选不同模型）。
        免密 lane 已删除（2026-11-12）：历史 "free:<model>" 路由自动改走号池同名模型，
        不再调用已下线的 free_chat。无路由/失败抛异常，由调用方回落本地。
        """
        spec = str((self._task_routes_overlay()
                    or self._config.model_router.task_routes or {}).get(task) or "")
        if not spec:
            raise RuntimeError(f"任务 {task} 未配置远程路由")
        if spec.startswith(("pool:", "free:")):
            pool = self._account_pool
            if pool is None:
                raise RuntimeError("号池未装配")
            return pool.complete(prompt, model=spec.split(":", 1)[1])
        if spec.startswith("cloud:"):
            # 注册表供应商 + 指定模型：base_url 从 provider_registry 查，
            # key 读供应商级 secret model_router.<pid>（一次填写全用途共用）。
            rest = spec.split(":", 1)[1]
            pid, _, model_id = rest.partition("|")
            pid, model_id = pid.strip(), model_id.strip()
            if not pid or not model_id:
                raise RuntimeError(f"非法 cloud 路由 spec：{spec}")
            base_url = ""
            try:
                from gateway.account_pool.catalog import provider_registry
                for p in provider_registry():
                    if str(p.get("id") or "") == pid:
                        base_url = str(p.get("base_url") or "")
                        break
            except Exception:  # noqa: BLE001
                base_url = ""
            if not base_url:
                raise RuntimeError(f"注册表无供应商 {pid}")
            api_key = ""
            if self._secret_store is not None:
                api_key = self._secret_store.get(f"model_router.{pid}") or ""
            if not api_key:
                raise RuntimeError(f"供应商 {pid} 未配置 API Key")
            tmp = ModelClient(name=f"{pid}:{model_id}", kind=CLOUD, endpoint=base_url,
                              model=model_id, api_key=api_key,
                              timeout_sec=float(self._config.model_router.request_timeout_sec))
            logger.info("[route] %s -> cloud %s|%s @ %s", task, pid, model_id, base_url)
            return tmp.complete(prompt)
        if spec.startswith("provider:"):
            name = spec.split(":", 1)[1]
            for client in self._pool:
                if client.name == name:
                    return client.complete(prompt)
            # 模型配置页「自定义/注册表模型」路径：base_url/model 写在 model.<name>_*，
            # key 在 secret model.<name>_api_key，未进 provider overlay 候选池。
            # 这里按需构造一个临时 OpenAI 兼容 client 发请求，让该路径真正生效。
            mc = self._config.model
            base_url = str(getattr(mc, f"{name}_base_url", "") or "")
            model_name = str(getattr(mc, f"{name}_model", "") or "")
            if base_url and model_name:
                api_key = ""
                if self._secret_store is not None:
                    api_key = self._secret_store.get(f"{name}_api_key") or ""
                if not api_key:
                    raise RuntimeError(
                        f"任务 {task} 选了云端模型 {model_name} 但 API Key 为空")
                tmp = ModelClient(name=name, kind=CLOUD, endpoint=base_url,
                                  model=model_name, api_key=api_key,
                                  timeout_sec=float(self._config.model_router.request_timeout_sec))
                return tmp.complete(prompt)
            raise RuntimeError(f"供应商不存在：{name}")
        raise RuntimeError(f"非法路由 spec：{spec}")

    def _complete_explicit(self, prompt: str, model: str, session_id: str,
                           *, images: list[str] | None = None) -> dict[str, Any]:
        """定向调用用户点名的模型（一律走号池；免密 lane 已删除）。"""
        pool = self._account_pool
        if pool is None:
            return {"ok": False, "text": "号池未装配（.env 设 POOL_ENABLED=true 并重启后端）",
                    "model": model, "kind": "pool", "fallback": True, "cache_hit": False}
        base = model.split(":", 1)[1] if model.startswith("free:") else model
        started = time.perf_counter()
        try:
            text = pool.complete(prompt, model=base, images=images)
            kind = "pool"
            return {"ok": True, "text": text, "model": base, "kind": kind,
                    "fallback": False, "cache_hit": False,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
        except Exception as exc:  # noqa: BLE001  定向失败如实报错，不换模型
            return {"ok": False, "text": f"调用 {base} 失败：{exc}",
                    "model": base, "kind": "pool", "fallback": True,
                    "cache_hit": False}


_router: ModelRouter | None = None


def get_model_router(config: BrainConfig | None = None, **kwargs: Any) -> ModelRouter:
    global _router
    if _router is None:
        _router = ModelRouter(config, **kwargs)
    return _router


def reset_model_router() -> None:
    global _router
    _router = None
