"""engine.embedding —— BGE-M3（稠密 1024 维 + 稀疏权重）。

运行时 ONNX Runtime；CUDA provider 探测失败即落 CPU（记降级标志，不抛）。
懒加载：首次 embed 才建 session，冷启动不预热（验收：冷启动 <30s）。

降级可测性（.memory/DECISIONS.md ADR-005）：provider 解析抽成纯函数
`resolve_onnx_providers()`，接受"可用 provider 列表"作为入参，测试不需要真 GPU。
"""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from core.errors import EngineUnavailable
from core.interfaces import Embedder, Embedding
from engine.cuda_runtime import prepare_cuda_dll_path

__all__ = [
    "BgeM3Embedder", "available_onnx_providers",
    "resolve_onnx_model_file", "resolve_onnx_providers", "special_token_ids",
]


def special_token_ids(tokenizer_file: str | Path) -> set[int]:
    """从 tokenizer.json 收集 special=true 的 added_tokens（<s>/<pad>/</s>/<unk>/<mask>）。

    BGE-M3 的 lexical 权重不应对特殊 token 计数；解析失败时回退到 XLM-R 的
    四个基础特殊 id，至少不会把 CLS/PAD 计入稀疏向量。
    """
    try:
        payload = json.loads(Path(tokenizer_file).read_text(encoding="utf-8"))
        ids = {
            int(token["id"])
            for token in payload.get("added_tokens", [])
            if token.get("special") and "id" in token
        }
    except (OSError, ValueError, TypeError, KeyError):
        ids = set()
    return ids or {0, 1, 2, 3}


def resolve_onnx_model_file(model: str | Path) -> Path | None:
    """把模型目录解析成实际的 .onnx 文件（权重可能在同目录的 model.onnx_data）。

    优先挑常规/量化命名，最后退回目录下任意 .onnx（排除临时导出的 graph.onnx）。
    """
    path = Path(model)
    if path.is_file():
        return path if path.suffix.lower() == ".onnx" else None
    if not path.is_dir():
        return None
    for name in ("model.onnx", "model_int8.onnx", "model_quant.onnx", "model_quantized.onnx"):
        candidate = path / name
        if candidate.is_file():
            return candidate
    others = sorted(p for p in path.glob("*.onnx") if p.name != "graph.onnx")
    return others[0] if others else None


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
        self._model_file = ""
        self._load_sec = 0.0
        self._max_length = 512
        self._input_names: set[str] = set()
        self._output_names: list[str] = []
        self._sparse_session: Any = None
        self._sparse_enabled = False
        self._sparse_error = ""
        self._special_ids: set[int] = set()
        # 一次写入里「丘脑去重」和「向量投影」会对同一段文本各 embed 一次；
        # 短 LRU 复用结果，也顺带加速重复查询。实例随内核重建，不会跨配置污染。
        self._cache: OrderedDict[str, Embedding] = OrderedDict()
        self._cache_max = 256

    @property
    def dense_dim(self) -> int:
        return self._dense_dim

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
    def load_sec(self) -> float:
        return self._load_sec

    @property
    def providers(self) -> list[str]:
        return list(self._providers)

    @property
    def load_error(self) -> str:
        return self._load_error

    @property
    def sparse_enabled(self) -> bool:
        return self._sparse_enabled

    @property
    def sparse_error(self) -> str:
        return self._sparse_error

    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        prepare_cuda_dll_path()
        try:
            import onnxruntime as ort
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"onnxruntime 不可用: {exc}"
            raise EngineUnavailable(self._load_error) from exc
        if not self._model_dir:
            self._load_error = "未配置 BGE-M3 ONNX 模型目录（config.model.embedding_model）"
            raise EngineUnavailable(self._load_error)
        model_file = resolve_onnx_model_file(self._model_dir)
        if model_file is None:
            self._load_error = f"{self._model_dir} 下找不到 .onnx 模型文件"
            raise EngineUnavailable(self._load_error)
        if not (Path(self._model_dir) / "tokenizer.json").is_file():
            self._load_error = f"缺少 tokenizer.json：{Path(self._model_dir) / 'tokenizer.json'}"
            raise EngineUnavailable(self._load_error)

        requested = resolve_onnx_providers(self._use_cuda, list(ort.get_available_providers()))
        started = time.perf_counter()
        options = ort.SessionOptions()
        options.log_severity_level = 3
        try:
            session = ort.InferenceSession(
                str(model_file), sess_options=options, providers=requested
            )
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"ONNX 模型加载失败: {type(exc).__name__}: {exc}"
            raise EngineUnavailable(self._load_error) from exc
        self._load_sec = time.perf_counter() - started
        self._model_file = str(model_file)
        self._session = session
        self._input_names = {item.name for item in session.get_inputs()}
        self._output_names = [item.name for item in session.get_outputs()]
        # 以 session 实际拿到的 provider 为准：CUDA 创建失败时 ORT 会静默回落 CPU
        self._providers = list(session.get_providers())
        self._degraded = "CUDAExecutionProvider" not in self._providers
        self._load_lexical_head(ort, options)

    def _load_lexical_head(self, ort: Any, options: Any) -> None:
        """加载 scripts/export_sparse_head.py 导出的 lexical 头，与主模型共用 provider。"""
        head = Path(self._model_dir or "") / "sparse_head.onnx"
        if not head.is_file():
            self._sparse_error = "缺少 sparse_head.onnx（运行 scripts/export_sparse_head.py 生成）"
            return
        try:
            self._sparse_session = ort.InferenceSession(
                str(head), sess_options=options, providers=self._providers
            )
        except Exception as exc:  # noqa: BLE001
            self._sparse_error = f"lexical 头加载失败: {type(exc).__name__}: {exc}"
            return
        self._special_ids = special_token_ids(Path(self._model_dir or "") / "tokenizer.json")
        self._sparse_enabled = True
        self._sparse_error = ""

    def embed_documents(self, texts: Sequence[str]) -> list[Embedding]:
        """带短 LRU 的批量向量化：相同文本只跑一次 ONNX。"""
        items = [str(text) for text in texts]
        if not items:
            return []
        results: dict[int, Embedding] = {}
        missing_pos: list[int] = []
        missing_text: list[str] = []
        for index, text in enumerate(items):
            hit = self._cache.get(text)
            if hit is not None:
                self._cache.move_to_end(text)
                results[index] = hit
            else:
                missing_pos.append(index)
                missing_text.append(text)
        if missing_text:
            for pos, text, embedding in zip(
                missing_pos, missing_text, self._embed_uncached(missing_text), strict=True
            ):
                results[pos] = embedding
                self._cache[text] = embedding
                if len(self._cache) > self._cache_max:
                    self._cache.popitem(last=False)
        return [results[index] for index in range(len(items))]

    def _embed_uncached(self, texts: Sequence[str]) -> list[Embedding]:
        """dense = L2 归一化的 sentence_embedding；sparse = lexical 头按 token max pooling。

        lexical 头来自官方 sparse_linear.pt，由 scripts/export_sparse_head.py 导出为
        sparse_head.onnx 并与主模型共用 CUDA provider；缺失时 sparse 降级为空字典，
        绝不拿 token_embeddings 编造权重。
        """
        if not texts:
            return []
        self._ensure_loaded()
        tokenizer = self._get_tokenizer()
        if tokenizer is None:
            raise EngineUnavailable(
                "缺少 BGE-M3 tokenizer（需 tokenizers 与模型目录配套：tokenizer.json）"
            )
        encodings = tokenizer.encode_batch([str(text) for text in texts])
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
        try:
            outputs = self._session.run(None, feeds)
        except Exception as exc:  # noqa: BLE001
            raise EngineUnavailable(f"BGE-M3 推理失败: {type(exc).__name__}: {exc}") from exc
        named = dict(zip(self._output_names, outputs))
        raw = named.get("sentence_embedding")
        if raw is None:
            raw = named.get("dense")
        if raw is None:
            raise EngineUnavailable(f"BGE-M3 缺少 dense 输出，实际输出: {self._output_names}")
        dense = np.asarray(raw, dtype=np.float32)
        if dense.ndim != 2 or dense.shape[0] != len(texts):
            raise EngineUnavailable(f"BGE-M3 dense 输出形状异常: {dense.shape}")
        if dense.shape[1] != self._dense_dim:
            raise EngineUnavailable(
                f"BGE-M3 输出 {dense.shape[1]} 维，与配置 dense_dim={self._dense_dim} 不符"
            )
        norms = np.linalg.norm(dense, axis=1, keepdims=True)
        dense = dense / np.maximum(norms, 1e-12)
        sparse_vectors = self._sparse_vectors(named, input_ids, attention_mask)
        return [
            Embedding(dense=[float(value) for value in row], sparse=sparse_vectors[index])
            for index, row in enumerate(dense)
        ]

    def _sparse_vectors(
        self,
        named: dict[str, Any],
        input_ids: np.ndarray,
        attention_mask: np.ndarray,
    ) -> list[dict[str, float]]:
        """token 级 lexical 权重 → {token_id: max_weight}，与官方 BGE-M3 一致。

        key 用 token id 字符串：query 与 doc 由同一个 tokenizer.json 编码，跨设备
        确定性一致；LanceVectorDB.search_sparse 正是按 key 做点积。
        """
        empty: list[dict[str, float]] = [{} for _ in range(input_ids.shape[0])]
        if not self._sparse_enabled or self._sparse_session is None:
            return empty
        hidden = named.get("token_embeddings")
        if hidden is None:
            self._sparse_error = "主模型未输出 token_embeddings，无法计算 lexical 权重"
            return empty
        try:
            (weights,) = self._sparse_session.run(
                None, {"token_embeddings": np.ascontiguousarray(hidden, dtype=np.float32)}
            )
        except Exception as exc:  # noqa: BLE001
            self._sparse_error = f"lexical 推理失败: {type(exc).__name__}: {exc}"
            return empty
        weights = np.asarray(weights, dtype=np.float32).reshape(input_ids.shape[0], -1)
        result: list[dict[str, float]] = []
        for row in range(input_ids.shape[0]):
            sparse: dict[str, float] = {}
            for pos in range(min(input_ids.shape[1], weights.shape[1])):
                if attention_mask[row, pos] == 0:
                    continue
                token_id = int(input_ids[row, pos])
                if token_id in self._special_ids:
                    continue
                weight = float(weights[row, pos])
                if weight <= 0.0:
                    continue
                key = str(token_id)
                if weight > sparse.get(key, 0.0):
                    sparse[key] = weight
            result.append(sparse)
        return result

    def _get_tokenizer(self) -> Any:
        if self._tokenizer is not None:
            return self._tokenizer
        try:
            from tokenizers import Tokenizer  # type: ignore[import-not-found]

            tokenizer = Tokenizer.from_file(str(Path(self._model_dir) / "tokenizer.json"))
            tokenizer.enable_truncation(max_length=self._max_length)
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"BGE-M3 tokenizer 加载失败: {type(exc).__name__}: {exc}"
            return None
        self._tokenizer = tokenizer
        return self._tokenizer
