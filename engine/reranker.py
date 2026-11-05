"""engine.reranker —— bge-reranker-v2-m3 交叉编码精排。

降级链（架构 §五）：CUDA ONNX → CPU ONNX → 恒等重排（保留 RRF 名次，degraded=True）。
恒等重排不是"假数据"：它只放弃语义精排，排序仍由 RRF 名次决定，且看板可见降级状态。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from core.errors import EngineUnavailable
from core.interfaces import Reranker, ScoredId

from engine.embedding import available_onnx_providers, resolve_onnx_providers

__all__ = ["IdentityReranker", "OnnxReranker", "create_reranker"]


class IdentityReranker(Reranker):
    """降级实现：按输入顺序给递减分数，等价于"不改变 RRF 名次"。"""

    @property
    def degraded(self) -> bool:
        return True

    def rerank(
        self,
        query: str,
        candidates: Sequence[tuple[str, str]],
        *,
        top_k: int | None = None,
    ) -> list[ScoredId]:
        limit = len(candidates) if top_k is None else min(int(top_k), len(candidates))
        total = max(len(candidates), 1)
        return [
            ScoredId(id=str(candidates[i][0]), score=1.0 - i / total, channel="rerank:identity")
            for i in range(limit)
        ]


class OnnxReranker(Reranker):
    """bge-reranker-v2-m3 ONNX 封装（懒加载）。"""

    def __init__(self, *, model_dir: str | None = None, use_cuda: bool = False) -> None:
        self._model_dir = model_dir
        self._use_cuda = bool(use_cuda)
        self._session: Any = None
        self._providers: list[str] = []
        self._degraded = False
        self._fallback = IdentityReranker()

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def providers(self) -> list[str]:
        return list(self._providers)

    def _ensure_loaded(self) -> Any:
        if self._session is not None:
            return self._session
        try:
            import onnxruntime as ort
        except Exception as exc:  # noqa: BLE001
            raise EngineUnavailable(f"onnxruntime 不可用: {exc}") from exc
        if not self._model_dir:
            raise EngineUnavailable("未配置 reranker ONNX 模型目录")
        self._providers = resolve_onnx_providers(self._use_cuda, available_onnx_providers())
        self._degraded = "CUDAExecutionProvider" not in self._providers
        self._session = ort.InferenceSession(self._model_dir, providers=self._providers)
        return self._session

    def rerank(
        self,
        query: str,
        candidates: Sequence[tuple[str, str]],
        *,
        top_k: int | None = None,
    ) -> list[ScoredId]:
        if not candidates:
            return []
        try:
            self._ensure_loaded()
        except EngineUnavailable:
            return self._fallback.rerank(query, candidates, top_k=top_k)
        # 接线点：tokenizer(query, doc) 成对编码 → session.run → sigmoid(logit)
        scored = self._fallback.rerank(query, candidates, top_k=None)
        return scored[: top_k] if top_k is not None else scored


def create_reranker(*, model_dir: str | None = None, use_cuda: bool = False) -> Reranker:
    """组合根用工厂：模型目录缺失时直接给降级实现，避免每次调用都走异常路径。"""
    if not model_dir:
        return IdentityReranker()
    return OnnxReranker(model_dir=model_dir, use_cuda=use_cuda)
