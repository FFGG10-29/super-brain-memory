"""engine.embedding —— BGE-M3（稠密 1024 维 + 稀疏权重）。

运行时 ONNX Runtime；CUDA provider 探测失败即落 CPU（记降级标志，不抛）。
懒加载：首次 embed 才建 session，冷启动不预热（验收：冷启动 <30s）。

降级可测性（.memory/DECISIONS.md ADR-005）：provider 解析抽成纯函数
`resolve_onnx_providers()`，接受"可用 provider 列表"作为入参，测试不需要真 GPU。
"""
from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from core.errors import EngineUnavailable
from core.interfaces import Embedder, Embedding

__all__ = ["BgeM3Embedder", "available_onnx_providers", "resolve_onnx_providers"]


def resolve_onnx_providers(requested_cuda: bool, available: Sequence[str]) -> list[str]:
    """纯函数：决定 provider 顺序。requested_cuda 但没有 CUDA provider → 静默落 CPU。"""
    have = set(available)
    providers: list[str] = []
    if requested_cuda and "CUDAExecutionProvider" in have:
        providers.append("CUDAExecutionProvider")
    if "CPUExecutionProvider" in have:
        providers.append("CPUExecutionProvider")
    if not providers:
        # onnxruntime 一定自带 CPU；走到这里说明环境异常，仍给出 CPU 让上层报明确错误
        providers.append("CPUExecutionProvider")
    return providers


def available_onnx_providers() -> list[str]:
    """真实探测（懒加载：onnxruntime 未装时返回空列表，不抛）。"""
    try:
        import onnxruntime as ort
    except Exception:  # noqa: BLE001
        return []
    try:
        return list(ort.get_available_providers())
    except Exception:  # noqa: BLE001
        return []


class BgeM3Embedder(Embedder):
    """BGE-M3 ONNX 封装。session 与 tokenizer 都在首次调用时创建。"""

    def __init__(
        self,
        *,
        dense_dim: int,
        model_dir: str | None = None,
        use_cuda: bool = False,
    ) -> None:
        self._dense_dim = int(dense_dim)
        self._model_dir = model_dir
        self._use_cuda = bool(use_cuda)
        self._session: Any = None
        self._tokenizer: Any = None
        self._providers: list[str] = []
        self._degraded = False
        self._load_error: str = ""

    @property
    def dense_dim(self) -> int:
        return self._dense_dim

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def providers(self) -> list[str]:
        return list(self._providers)

    @property
    def load_error(self) -> str:
        return self._load_error

    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        try:
            import onnxruntime as ort
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"onnxruntime 不可用: {exc}"
            raise EngineUnavailable(self._load_error) from exc
        if not self._model_dir:
            self._load_error = "未配置 BGE-M3 ONNX 模型目录（config.model.embedding_model）"
            raise EngineUnavailable(self._load_error)

        available = list(ort.get_available_providers())
        self._providers = resolve_onnx_providers(self._use_cuda, available)
        # 请求了 CUDA 但没拿到 → 标记降级（看板与日志据此告警）
        self._degraded = "CUDAExecutionProvider" not in self._providers
        started = time.perf_counter()
        self._session = ort.InferenceSession(self._model_dir, providers=self._providers)
        _ = time.perf_counter() - started

    def embed_documents(self, texts: Sequence[str]) -> list[Embedding]:
        self._ensure_loaded()
        if self._get_tokenizer() is None:
            raise EngineUnavailable(
                "缺少 BGE-M3 tokenizer（需 tokenizers 与模型目录配套：tokenizer.json）"
            )
        # 接线点：tokenizer.encode_batch(texts) → session.run(input_ids/attention_mask)
        #          → dense 取 [CLS] 归一化，sparse 取 ReLU(weights) 的非零项
        raise EngineUnavailable(
            "BGE-M3 推理尚未接线：模型文件就位后在此实现（session.run + 稀疏权重抽取）"
        )

    def _get_tokenizer(self) -> Any:
        if self._tokenizer is not None:
            return self._tokenizer
        try:
            from tokenizers import Tokenizer  # type: ignore[import-not-found]

            self._tokenizer = Tokenizer.from_file(f"{self._model_dir}/tokenizer.json")
        except Exception:  # noqa: BLE001
            self._tokenizer = None
        return self._tokenizer
