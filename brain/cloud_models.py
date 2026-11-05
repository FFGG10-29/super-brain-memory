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

__all__ = ["CloudCredentials", "CloudEmbedder", "CloudLLM", "load_cloud_api_key"]

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def load_cloud_api_key(config, channel: str, *, keyring_backend: Any | None = None) -> str:
    """按通道（embed / llm）读 API key：钥匙串 > 各自环境变量；都没有返回空串。"""
    if keyring_backend is not None:
        try:
            value = keyring_backend.get_password("super-brain-v21", f"{channel}_api_key")
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
    """云端暂无标准 rerank API：恒等占位（保持 RRF 顺序），等价于关闭精排。"""

    degraded = True

    def rerank(self, query, candidates, *, top_k=None) -> list[ScoredId]:
        items = [ScoredId(id=cid, score=1.0 / (rank + 1), channel="cloud-passthrough")
                 for rank, (cid, _text) in enumerate(candidates)]
        return items[:top_k] if top_k else items
