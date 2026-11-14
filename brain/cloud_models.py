"""brain.cloud_models —— 云端算力层：OpenAI 兼容 API 的 Embedder / LLM 实现。

契约与本地模型完全一致（core.interfaces），组合根按 model_mode 选择装配：
- cloud 模式：embedder/llm 走 HTTP（urllib，无 SDK 依赖）；
- 任何网络/格式失败抛 EngineUnavailable → 上层既有降级路径接管
  （召回跳过向量通道、抽取退规则），不会让请求挂死。
API key 读取顺序：系统钥匙串（account=cloud_api_key）> 环境变量；永不写配置文件/事件日志。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

from core.errors import EngineUnavailable
from core.interfaces import Embedder, Embedding, LocalLLM, Reranker, ScoredId

__all__ = [
    "CloudCredentials", "CloudEmbedder", "CloudLLM", "CloudReranker",
    "FallbackEmbedder", "FallbackReranker", "FallbackLLM",
    "load_cloud_api_key", "SILICONFLOW_BASE_URL",
]

# 硅基流动 OpenAI 兼容端点；免费模型 BAAI/bge-m3（embedding）+ BAAI/bge-reranker-v2-m3（rerank）
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_EMBED_MODEL = "BAAI/bge-m3"
SILICONFLOW_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def load_cloud_api_key(config, channel: str, *, keyring_backend: Any | None = None) -> str:
    """按通道（embed / llm）读 API key：钥匙串 > 各自环境变量；都没有返回空串。"""
    # 单一真源：与前端「模型配置 → secret.set」写入共用 default_secret_store
    # （service=super-brain-v3 / DPAPI 加密文件），此前读 super-brain-v21 导致
    # 前端存的 key 永远读不到、云端模型配了用不了。
    try:
        from core.desktop_features import default_secret_store

        backend = keyring_backend or default_secret_store()

        def _read(account: str) -> str:
            if keyring_backend is not None and hasattr(backend, "get_password"):
                return str(backend.get_password("super-brain-v3", account) or "")
            return str(backend.get(account) or "")

        # ① 本通道专用 key（模型配置页 embedding/rerank 输入框写的）
        value = _read(f"{channel}_api_key")
        if value:
            return value
        # ② 回退：号池页「云供应商」填的 key（model_router.<provider_id>）。
        #    按当前云端 base_url 反查供应商 id，避免同一家 key 要在两处各填一遍。
        base_url = str(getattr(config.model, f"{channel}_base_url", "") or "").lower()
        pid = ""
        if "siliconflow" in base_url:
            pid = "siliconflow"
        elif "openrouter" in base_url:
            pid = "open_router"
        elif "volces.com" in base_url or "volcengine" in base_url:
            pid = "volcengine-ark"
        elif "byteplus" in base_url:
            pid = "byteplus"
        if pid:
            value = _read(f"model_router.{pid}")
            if value:
                return value
    except Exception:  # noqa: BLE001
        pass
    env_name = getattr(config.model, f"{channel}_api_key_env", "")
    return os.environ.get(env_name, "")


class CloudCredentials:
    def __init__(self, base_url: str, api_key: str, timeout: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise EngineUnavailable(f"云端 API 请求失败：{exc}") from exc
        if time.perf_counter() - started > self.timeout:
            raise EngineUnavailable("云端 API 响应超时")
        return data


class CloudEmbedder(Embedder):
    """OpenAI 兼容 /embeddings；向量维度必须与 dense_dim 一致（建表时就锁定了）。"""

    def __init__(
        self,
        credentials: CloudCredentials,
        model: str,
        *,
        dense_dim: int,
    ) -> None:
        self._credentials = credentials
        self._model = model
        self._dense_dim = dense_dim

    @property
    def dense_dim(self) -> int:
        return self._dense_dim

    @property
    def degraded(self) -> bool:
        return False

    def embed_documents(self, texts: Sequence[str]) -> list[Embedding]:
        if not texts:
            return []
        data = self._credentials.post(
            "/embeddings", {"model": self._model, "input": list(texts)}
        )
        try:
            items = sorted(data["data"], key=lambda item: item["index"])
            vectors = [[float(value) for value in item["embedding"]] for item in items]
        except (KeyError, TypeError, ValueError) as exc:
            raise EngineUnavailable(f"云端 embedding 返回格式异常：{exc}") from exc
        if len(vectors) != len(texts):
            raise EngineUnavailable("云端 embedding 数量与输入不一致")
        for vector in vectors:
            if len(vector) != self._dense_dim:
                raise EngineUnavailable(
                    f"云端向量维度 {len(vector)} 与 dense_dim={self._dense_dim} 不一致，"
                    "请调整 model.cloud_embed_dim（注意：换维度需重建向量库）"
                )
        return [Embedding(dense=vector) for vector in vectors]


class CloudLLM(LocalLLM):
    """OpenAI 兼容 /chat/completions；强制 JSON 输出与 LocalLLM 同一契约。"""

    degraded = False

    def __init__(
        self,
        credentials: CloudCredentials,
        model: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> None:
        self._credentials = credentials
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        required_keys: Sequence[str] = (),
        max_retries: int = 2,
    ) -> Mapping[str, Any]:
        last_error: Exception | None = None
        for _ in range(max_retries + 1):
            try:
                data = self._credentials.post(
                    "/chat/completions",
                    {
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": self._temperature,
                        "max_tokens": self._max_tokens,
                        "response_format": {"type": "json_object"},
                    },
                )
                content = data["choices"][0]["message"]["content"]
                parsed = self._parse_json(str(content))
                missing = [key for key in required_keys if key not in parsed]
                if not missing:
                    return parsed
                last_error = EngineUnavailable(f"云端返回缺少字段：{missing}")
            except EngineUnavailable as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        try:
            return dict(json.loads(content))
        except json.JSONDecodeError:
            match = _JSON_BLOCK.search(content)
            if match is None:
                raise EngineUnavailable("云端 LLM 未返回 JSON") from None
            return dict(json.loads(match.group(0)))


class CloudReranker(Reranker):
    """真·云端交叉编码精排：硅基流动 /v1/rerank（BAAI/bge-reranker-v2-m3，免费）。

    请求 {model, query, documents, top_n, return_documents:false}；
    响应 results:[{index, relevance_score}] 已按分数降序，index 指原始 documents 下标。
    任何网络/格式失败抛 EngineUnavailable → FallbackReranker 接管回落本地 ONNX。
    """

    degraded = False

    def __init__(self, credentials: CloudCredentials, model: str) -> None:
        self._credentials = credentials
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def rerank(self, query, candidates, *, top_k=None) -> list[ScoredId]:
        pairs = list(candidates)
        if not pairs:
            return []
        docs = [str(text) for _, text in pairs]
        payload: dict[str, Any] = {
            "model": self._model, "query": str(query),
            "documents": docs, "return_documents": False,
        }
        if top_k is not None:
            payload["top_n"] = int(top_k)
        try:
            data = self._credentials.post("/rerank", payload)
            results = data["results"]
            out: list[ScoredId] = []
            for row in results:
                idx = int(row["index"])
                if 0 <= idx < len(pairs):
                    out.append(ScoredId(id=str(pairs[idx][0]),
                                        score=float(row["relevance_score"]),
                                        channel="cloud-rerank"))
            if not out:
                raise EngineUnavailable("云端 rerank 返回空 results")
            return out
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise EngineUnavailable(f"云端 rerank 返回格式异常：{exc}") from exc


# ── 云端优先 + 失败回落本地（每个用途统一降级策略）─────────────────────
# 设计：构造时不预热本地模型（省内存/显存）；云端首次调用成功就一直用云端，
# 任一运行期失败（超时/限流/401/格式错）立即切本地并熔断 N 秒——熔断期内不再试云端，
# 避免每条查询都白等一次超时。熔断到期后半探针式重试，云端恢复则自动切回。

class _Circuit:
    """三态熔断器：CLOSED(试云端) → OPEN(熔断,直接本地) → HALF_OPEN(半探针,恢复则切回)。

    - CLOSED：每次调用都试云端；连续失败 threshold 次 → OPEN。
    - OPEN：cooldown 秒内不试云端（直接用本地，避免每条白等超时）；到期 → HALF_OPEN。
    - HALF_OPEN：放行一次云端探针；成功 → 回 CLOSED（自动切回云端），失败 → 回 OPEN 重新计时。
    """

    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, threshold: int = 2, cooldown: float = 60.0) -> None:
        self._threshold = max(1, int(threshold))
        self._cooldown = float(cooldown)
        self._fails = 0
        self._open_until = 0.0
        self._state = self.CLOSED

    @property
    def state(self) -> str:
        if self._state == self.OPEN and time.monotonic() >= self._open_until:
            self._state = self.HALF_OPEN  # 熔断到期，放行一次探针
        return self._state

    def should_try_cloud(self) -> bool:
        return self.state in (self.CLOSED, self.HALF_OPEN)

    def ok(self) -> None:
        """云端调用成功：复位到 CLOSED（HALF_OPEN 探针成功=自动切回云端）。"""
        self._fails = 0
        self._open_until = 0.0
        self._state = self.CLOSED

    def fail(self) -> None:
        """云端调用失败：CLOSED 累计到阈值→OPEN；HALF_OPEN 探针失败→重新 OPEN。"""
        if self._state == self.HALF_OPEN:
            self._open_until = time.monotonic() + self._cooldown
            self._fails = 0
            self._state = self.OPEN
            return
        self._fails += 1
        if self._fails >= self._threshold:
            self._open_until = time.monotonic() + self._cooldown
            self._fails = 0
            self._state = self.OPEN


class FallbackEmbedder(Embedder):
    """云端 embedder 优先，EngineUnavailable 时回落本地 BGE-M3（懒加载）。"""

    def __init__(self, cloud: Embedder, local_factory: Any,
                 *, circuit_threshold: int = 2, circuit_cooldown: float = 60.0) -> None:
        self._cloud = cloud
        self._local_factory = local_factory  # callable() -> Embedder，首次回退时才构造
        self._local: Embedder | None = None
        self._circuit = _Circuit(circuit_threshold, circuit_cooldown)
        self._using_local = False

    @property
    def dense_dim(self) -> int:
        # 本地与云端维度必须一致（建表锁定）；回退后以实际生效者为准
        return (self._local.dense_dim if self._using_local and self._local is not None
                else self._cloud.dense_dim)

    @property
    def degraded(self) -> bool:
        return self._using_local or self._cloud.degraded

    @property
    def using_local(self) -> bool:
        return self._using_local

    @property
    def circuit_state(self) -> str:
        return self._circuit.state

    def _local_impl(self) -> Embedder:
        if self._local is None:
            self._local = self._local_factory()
        return self._local

    def embed_documents(self, texts: Sequence[str]) -> list[Embedding]:
        if self._circuit.should_try_cloud():  # CLOSED 或 HALF_OPEN 探针
            try:
                out = self._cloud.embed_documents(texts)
                self._circuit.ok()
                self._using_local = False  # 探针成功=自动切回云端
                return out
            except EngineUnavailable:
                self._circuit.fail()
                self._using_local = True   # 回落本地（OPEN 期不再试云端）
        else:
            self._using_local = True
        return self._local_impl().embed_documents(texts)


class FallbackReranker(Reranker):
    """云端 rerank 优先，失败回落本地 ONNX（懒加载）；恒等占位为最后防线。"""

    def __init__(self, cloud: Reranker, local_factory: Any,
                 *, circuit_threshold: int = 2, circuit_cooldown: float = 60.0) -> None:
        self._cloud = cloud
        self._local_factory = local_factory
        self._local: Reranker | None = None
        self._circuit = _Circuit(circuit_threshold, circuit_cooldown)
        self._using_local = False

    @property
    def degraded(self) -> bool:
        return self._using_local or self._cloud.degraded

    @property
    def using_local(self) -> bool:
        return self._using_local

    @property
    def circuit_state(self) -> str:
        return self._circuit.state

    def _local_impl(self) -> Reranker:
        if self._local is None:
            self._local = self._local_factory()
        return self._local

    def rerank(self, query, candidates, *, top_k=None) -> list[ScoredId]:
        if self._circuit.should_try_cloud():
            try:
                out = self._cloud.rerank(query, candidates, top_k=top_k)
                self._circuit.ok()
                self._using_local = False
                return out
            except EngineUnavailable:
                self._circuit.fail()
                self._using_local = True
        else:
            self._using_local = True
        return self._local_impl().rerank(query, candidates, top_k=top_k)


class FallbackLLM(LocalLLM):
    """云端 LLM 优先，失败回落本地 Qwen3-0.6B / DisabledLLM（懒加载）。"""

    degraded = False

    def __init__(self, cloud: LocalLLM, local_factory: Any,
                 *, circuit_threshold: int = 2, circuit_cooldown: float = 60.0) -> None:
        self._cloud = cloud
        self._local_factory = local_factory
        self._local: LocalLLM | None = None
        self._circuit = _Circuit(circuit_threshold, circuit_cooldown)
        self._using_local = False

    @property
    def using_local(self) -> bool:
        return self._using_local

    @property
    def circuit_state(self) -> str:
        return self._circuit.state

    def _local_impl(self) -> LocalLLM:
        if self._local is None:
            self._local = self._local_factory()
        return self._local

    def complete_json(self, system_prompt, user_prompt, *, required_keys=(), max_retries=2):
        if self._circuit.should_try_cloud():
            try:
                out = self._cloud.complete_json(
                    system_prompt, user_prompt, required_keys=required_keys, max_retries=max_retries)
                self._circuit.ok()
                self._using_local = False
                return out
            except EngineUnavailable:
                self._circuit.fail()
                self._using_local = True
        else:
            self._using_local = True
        return self._local_impl().complete_json(
            system_prompt, user_prompt, required_keys=required_keys, max_retries=max_retries)

    def complete_text(self, system_prompt, user_prompt) -> str:
        if self._circuit.should_try_cloud():
            try:
                out = self._cloud.complete_text(system_prompt, user_prompt)
                self._circuit.ok()
                self._using_local = False
                return out
            except EngineUnavailable:
                self._circuit.fail()
                self._using_local = True
        else:
            self._using_local = True
        return self._local_impl().complete_text(system_prompt, user_prompt)
