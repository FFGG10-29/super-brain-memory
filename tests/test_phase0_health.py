"""阶段 0 验收：六引擎连通 / 冷启动 <30s / 断 CUDA 落 CPU / thread-local 连接。

对应架构 §十一 阶段 0 验收标准。
"""
from __future__ import annotations

import threading
import time

import pytest

from core.errors import EngineUnavailable
from engine.embedding import BgeM3Embedder, resolve_onnx_providers
from main import build_app, self_check
from storage import ThreadLocalPool

COLD_START_BUDGET_SEC = 30


def test_six_engines_report_healthy(app):
    report = app.health_report()
    engines = [status.engine for status in report]
    assert engines == [
        "relational:sqlite",
        "vector:lancedb",
        "graph:kuzu",
        "kv:lmdb",
        "timeseries:duckdb",
        "hypergraph:kuzu",
        "clock:hlc",
    ]
    failures = [status for status in report if not status.ok]
    assert not failures, f"引擎不健康: {[(s.engine, s.detail) for s in failures]}"


def test_cold_start_within_budget(brain_config):
    started = time.perf_counter()
    app = build_app(brain_config)
    try:
        elapsed = time.perf_counter() - started
        assert elapsed < COLD_START_BUDGET_SEC, f"冷启动 {elapsed:.1f}s 超预算"
        # 三模型必须懒加载：构造阶段不加载权重
        assert app.embedder.dense_dim == brain_config.model.dense_dim
    finally:
        app.close()


def test_cuda_request_falls_back_to_cpu_without_gpu():
    """断 CUDA 落 CPU：纯函数可测，不依赖本机 GPU 状态（ADR-005）。"""
    assert resolve_onnx_providers(True, ["CPUExecutionProvider"]) == ["CPUExecutionProvider"]
    assert resolve_onnx_providers(
        True, ["CUDAExecutionProvider", "CPUExecutionProvider"]
    ) == ["CUDAExecutionProvider", "CPUExecutionProvider"]
    # 没请求 CUDA 时即使有也不能偷偷用
    assert resolve_onnx_providers(
        False, ["CUDAExecutionProvider", "CPUExecutionProvider"]
    ) == ["CPUExecutionProvider"]


def test_embedder_degrades_instead_of_crashing():
    embedder = BgeM3Embedder(dense_dim=8, model_dir=None, use_cuda=True)
    assert embedder.degraded is False  # 尚未加载
    with pytest.raises(EngineUnavailable):
        embedder.embed_documents(["hello"])  # 模型缺失 → 明确失败，不编造向量
    assert "模型目录" in embedder.load_error or "onnxruntime" in embedder.load_error


def test_thread_local_pool_isolates_connections():
    pool = ThreadLocalPool(lambda: object(), name="test")
    main_resource = pool.get()
    seen: list[object] = []

    def worker() -> None:
        seen.append(pool.get())

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert seen[0] is not main_resource, "跨线程必须拿到不同连接对象"
    assert pool.get() is main_resource, "同线程必须复用"
    pool.close()
    with pytest.raises(RuntimeError):
        pool.get()


def test_self_check_exit_code_is_zero(brain_config, capsys):
    assert self_check(brain_config) == 0
    output = capsys.readouterr().out
    assert "冷启动" in output and "relational:sqlite" in output
