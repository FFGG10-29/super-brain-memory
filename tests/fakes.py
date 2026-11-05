"""测试替身：确定性 Embedder / LocalLLM。

原则：替身只提供"确定性数值"，不改变被测逻辑；任何降级路径都由真实缺模型触发，
不靠替身伪造降级（ADR-005）。
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from core.interfaces import Embedder, Embedding, LocalLLM

DIM = 8


class FakeEmbedder(Embedder):
    """字符袋哈希到固定维度 + 字符 2-gram 稀疏权重，跨进程确定性。"""

    def __init__(self, dense_dim: int = DIM) -> None:
        self._dim = dense_dim

    @property
    def dense_dim(self) -> int:
        return self._dim

    def embed_documents(self, texts: Sequence[str]) -> list[Embedding]:
        return [self._embed(text) for text in texts]

    def _embed(self, text: str) -> Embedding:
        vector = [0.0] * self._dim
        for char in text:
            digest = hashlib.blake2b(char.encode("utf-8"), digest_size=2).digest()
            vector[int.from_bytes(digest, "big") % self._dim] += 1.0
        norm = sum(value * value for value in vector) ** 0.5 or 1.0
        dense = [value / norm for value in vector]
        sparse: dict[str, float] = {}
        cleaned = "".join(text.split())
        for index in range(max(1, len(cleaned) - 1)):
            sparse[cleaned[index : index + 2]] = 1.0
        return Embedding(dense=dense, sparse=sparse)


class FakeLLM(LocalLLM):
    """按脚本返回固定 JSON；脚本为空时抛 EngineUnavailable 走规则兜底。"""

    def __init__(self, payload: Mapping[str, Any] | None = None) -> None:
        self._payload = payload

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        required_keys: Sequence[str] = (),
        max_retries: int = 2,
    ) -> Mapping[str, Any]:
        from core.errors import EngineUnavailable

        if self._payload is None:
            raise EngineUnavailable("测试：未配置模型")
        for key in required_keys:
            if key not in self._payload:
                raise ValueError(f"缺少必需字段: {key}")
        return self._payload
