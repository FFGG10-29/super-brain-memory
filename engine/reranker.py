"""engine.reranker —— bge-reranker-v2-m3 交叉编码精排。

降级链（架构 §五）：CUDA ONNX → CPU ONNX → 恒等重排（保留 RRF 名次，degraded=True）。
恒等重排不是"假数据"：它只放弃语义精排，排序仍由 RRF 名次决定，且看板可见降级状态。
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from core.errors import EngineUnavailable
from core.interfaces import Reranker, ScoredId

from engine.cuda_runtime import prepare_cuda_dll_path
from engine.embedding import (
    available_onnx_providers,
    resolve_onnx_model_file,
    resolve_onnx_providers,
)

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
        self._tokenizer: Any = None
        self._providers: list[str] = []
        self._degraded = False
        self._fallback = IdentityReranker()
        self._load_error = ""
        self._model_file = ""
        self._max_length = 512
        self._input_names: set[str] = set()

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def loaded(self) -> bool:
        return self._session is not None

    @property
    def model_file(self) -> str:
        return self._model_file

    @property
    def load_error(self) -> str:
        return self._load_error

    @property
    def providers(self) -> list[str]:
        return list(self._providers)

    def _ensure_loaded(self) -> Any:
        if self._session is not None:
            return self._session
        prepare_cuda_dll_path()
        try:
            import onnxruntime as ort
        except Exception as exc:  # noqa: BLE001
            raise EngineUnavailable(f"onnxruntime 不可用: {exc}") from exc
        model_file = resolve_onnx_model_file(self._model_dir or "")
        if model_file is None:
            raise EngineUnavailable(f"{self._model_dir} 下找不到 reranker .onnx 模型文件")
        if not (Path(self._model_dir or "") / "tokenizer.json").is_file():
            raise EngineUnavailable("缺少 reranker tokenizer.json")
        requested = resolve_onnx_providers(self._use_cuda, available_onnx_providers())
        options = ort.SessionOptions()
        options.log_severity_level = 3
        try:
            session = ort.InferenceSession(
                str(model_file), sess_options=options, providers=requested
            )
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"ONNX 模型加载失败: {type(exc).__name__}: {exc}"
            raise EngineUnavailable(self._load_error) from exc
        self._session = session
        self._model_file = str(model_file)
        self._input_names = {item.name for item in session.get_inputs()}
        self._providers = list(session.get_providers())
        self._degraded = "CUDAExecutionProvider" not in self._providers
        return session

    def _get_tokenizer(self) -> Any:
        if self._tokenizer is not None:
            return self._tokenizer
        try:
            from tokenizers import Tokenizer  # type: ignore[import-not-found]

            tokenizer = Tokenizer.from_file(str(Path(self._model_dir) / "tokenizer.json"))
            tokenizer.enable_truncation(max_length=self._max_length)
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"reranker tokenizer 加载失败: {type(exc).__name__}: {exc}"
            return None
        self._tokenizer = tokenizer
        return self._tokenizer

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
            session = self._ensure_loaded()
            tokenizer = self._get_tokenizer()
            if tokenizer is None:
                raise EngineUnavailable("缺少 reranker tokenizer.json")
            encodings = tokenizer.encode_batch([(query, str(text)) for _, text in candidates])
            max_len = max(len(item.ids) for item in encodings)
            input_ids = np.zeros((len(encodings), max_len), dtype=np.int64)
            attention_mask = np.zeros_like(input_ids)
            type_ids = np.zeros_like(input_ids)
            for row, item in enumerate(encodings):
                size = len(item.ids)
                input_ids[row, :size] = item.ids
                attention_mask[row, :size] = item.attention_mask
                type_ids[row, :size] = item.type_ids
            feeds: dict[str, Any] = {"input_ids": input_ids, "attention_mask": attention_mask}
            if "token_type_ids" in self._input_names:
                feeds["token_type_ids"] = type_ids
            outputs = session.run(None, feeds)
            logits = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
            if logits.size != len(candidates):
                raise EngineUnavailable(f"reranker 输出形状异常: {logits.shape}")
            scores = 1.0 / (1.0 + np.exp(-np.clip(logits, -30.0, 30.0)))
            result = [
                ScoredId(
                    id=str(candidates[int(index)][0]),
                    score=float(scores[int(index)]),
                    channel="rerank:onnx",
                )
                for index in np.argsort(-scores)
            ]
            return result[:top_k] if top_k is not None else result
        except Exception as exc:  # noqa: BLE001  推理失败一律退回恒等重排
            self._load_error = f"reranker 推理失败: {type(exc).__name__}: {exc}"
            self._degraded = True
            return self._fallback.rerank(query, candidates, top_k=top_k)


def create_reranker(*, model_dir: str | None = None, use_cuda: bool = False) -> Reranker:
    """组合根工厂：目录缺失时回退 ``models_dir()`` 完整探测链，仍找不到才降级。

    2026-11-12 修复（P0-1）：旧实现 ``if not model_dir: return IdentityReranker()``
    在裸命令启动（未注入 SUPER_BRAIN_MODELS_DIR）时，``_resolve_model_dir`` 返回 None
    就让精排永久摆烂，而 embedder 靠 BgeM3Embedder 内部找模型仍 degraded=false——
    两条路径不一致。现按 models_dir() 的「环境变量 > 随包 models/ > 用户目录 >
    dev data/models」统一解析，找到 reranker 子目录才实例化真模型。
    """
    if not model_dir:
        try:
            from core.runtime_paths import models_dir

            root = models_dir()
            for candidate in (
                root / "BAAI" / "bge-reranker-v2-m3",
                root / "bge-reranker-v2-m3",
                root,
            ):
                if candidate.is_dir() and resolve_onnx_model_file(str(candidate)) is not None:
                    model_dir = str(candidate)
                    break
        except Exception:  # noqa: BLE001  探测失败回退降级，不影响启动
            pass
    if not model_dir:
        return IdentityReranker()
    return OnnxReranker(model_dir=model_dir, use_cuda=use_cuda)
