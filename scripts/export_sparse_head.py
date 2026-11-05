"""scripts.export_sparse_head —— 把 BGE-M3 的 lexical 头导出成独立 ONNX 模型。

官方 BAAI/bge-m3 的 onnx/model.onnx 只导出 sentence_embedding 与 token_embeddings，
没有 lexical 权重；但仓库另有 sparse_linear.pt（nn.Linear(1024, 1) 的 weight/bias）。
本脚本把它转成

    token_weights = ReLU(token_embeddings @ W.T + b)      # [B, L, 1] -> [B, L]

导出为 sparse_head.onnx，运行时由 BgeM3Embedder 用同一个 CUDA provider 执行，
再按 token id 做 max pooling 得到稀疏向量。

用法：.venv\\Scripts\\python.exe scripts/export_sparse_head.py [模型目录]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

__all__ = ["export", "load_linear"]

_HIDDEN = 1024


def load_linear(model_dir: Path) -> tuple[np.ndarray, np.ndarray]:
    """优先读 npz（venv 无需 torch）；否则从 sparse_linear.pt 转换并缓存 npz。"""
    npz = model_dir / "sparse_linear.npz"
    if npz.exists():
        with np.load(str(npz)) as data:
            return np.array(data["weight"], dtype=np.float32), np.array(data["bias"], dtype=np.float32)
    source = model_dir / "sparse_linear.pt"
    if not source.is_file():
        raise FileNotFoundError(f"缺少 {source}（从 BAAI/bge-m3 下载）")
    try:
        import torch  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "当前环境没有 torch：请用装了 torch 的解释器把 sparse_linear.pt 转成 "
            "sparse_linear.npz（weight/bias 两个 float32 数组）后重试"
        ) from exc
    state = torch.load(str(source), map_location="cpu", weights_only=True)
    weight = np.asarray(state["weight"].detach().to(torch.float32).numpy(), dtype=np.float32)
    bias = np.asarray(state["bias"].detach().to(torch.float32).numpy(), dtype=np.float32)
    np.savez(str(npz), weight=weight, bias=bias)
    return weight, bias


def export(model_dir: Path) -> Path:
    weight, bias = load_linear(model_dir)
    if weight.shape != (1, _HIDDEN) or bias.shape != (1,):
        raise ValueError(f"lexical 头形状异常: weight={weight.shape} bias={bias.shape}")
    initializers = [
        numpy_helper.from_array(weight.T.copy(), name="lexical_weight"),  # [1024, 1]
        numpy_helper.from_array(bias.copy(), name="lexical_bias"),
        numpy_helper.from_array(np.array([2], dtype=np.int64), name="squeeze_axes"),
    ]
    graph = helper.make_graph(
        [
            helper.make_node("MatMul", ["token_embeddings", "lexical_weight"], ["proj"]),
            helper.make_node("Add", ["proj", "lexical_bias"], ["biased"]),
            helper.make_node("Relu", ["biased"], ["activated"]),
            helper.make_node("Squeeze", ["activated", "squeeze_axes"], ["token_weights"]),
        ],
        "bge_m3_lexical_head",
        [helper.make_tensor_value_info("token_embeddings", TensorProto.FLOAT, ["batch", "sequence", _HIDDEN])],
        [helper.make_tensor_value_info("token_weights", TensorProto.FLOAT, ["batch", "sequence"])],
        initializer=initializers,
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.checker.check_model(model)
    target = model_dir / "sparse_head.onnx"
    onnx.save(model, str(target))
    return target


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parents[1]
    model_dir = Path(args[0]) if args else root / "data" / "models" / "BAAI" / "bge-m3"
    path = export(model_dir)
    print(f"已导出 lexical 头：{path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
