"""main.py —— 唯一组合根：DI、懒加载、优雅启停。

职责边界：
- 只在这里做具体实现的选择与装配（config → 六引擎 → 三模型 → brain → gateway）。
- brain/ 及以下层禁止反向 import 具体实现，只认 core.interfaces。
- 启动顺序：config 校验 → 六引擎连接/schema 校验 → 算力层（懒加载，不预热）
            → sync.git_worker（后台）→ gateway → workers。
- 启停：SIGINT/SIGTERM → drain（停接新请求）→ 停 worker → flush journal → 关六引擎。

当前进度（阶段 0）：六引擎 + 三模型 + HLC 装配完成，`python main.py --self-check` 可自检。
网关/同步/生命循环在阶段 3~5 接入，此处保留清晰的接线位置。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from brain.amygdala import Amygdala
from brain.hippocampus import Hippocampus
from brain.memory_consolidator import MemoryConsolidator
from brain.memory_tier_manager import MemoryTierManager
from brain.neocortex import Neocortex
from brain.prefrontal import Prefrontal
from brain.router_extractor import RouterExtractor
from brain.synaptic_plasticity import SynapticPlasticity
from brain.thalamus import Thalamus
from config import BrainConfig, get_config
from core.ids import HybridLogicalClock
from core.structured_log import configure_logging
from core.interfaces import (
    Embedder,
    GraphRepository,
    HealthCheckable,
    HealthStatus,
    HypergraphRepository,
    KVRepository,
    LocalLLM,
    RelationalRepository,
    Reranker,
    TimeSeriesRepository,
    VectorRepository,
)
from engine.cuda_runtime import prepare_cuda_dll_path
from engine.embedding import BgeM3Embedder
from engine.local_llm import QwenLocalLLM, DisabledLLM
from engine.reranker import create_reranker
from gateway.http_adapter import HTTPAdapter
from gateway.mcp_server import MCPToolkit
from gateway.registry import AgentRegistry
from storage.disabled import (
    DisabledGraph,
    DisabledHypergraph,
    DisabledKV,
    DisabledTimeSeries,
    DisabledVector,
)
from storage.graph_db import KuzuGraphDB
from storage.hypergraph_db import GraphHypergraph
from storage.kv_store import LmdbKVStore
from storage.relational_db import SqliteRelationalDB
from storage.time_series_db import DuckDBTimeSeries
from storage.vector_db import LanceVectorDB
from sync.event_store import EventStore
from sync.journal import NdjsonJournal
from sync.projections import EventProjector
from sync.snapshot import SnapshotManager
from sync.git_worker import GitSyncWorker
from workers.circadian import Circadian
from workers.life_loop import LifeLoop

__all__ = [
    "BrainApp", "StorageSet", "build_app", "build_storage",
    "gpu_runtime_info", "probe_models", "self_check",
]

# 冻结态下 CUDA DLL 随包放在 _MEIPASS/cuda_bin；必须在任何 import llama_cpp /
# onnxruntime 之前登记，否则 LoadLibrary 找不到 cuDNN 子库，会静默回落 CPU。
prepare_cuda_dll_path()

_PROCESS_START = time.perf_counter()


def _last_hlc(journal) -> str | None:
    stamp = journal.last_stamp()
    return stamp.to_token() if stamp is not None else None


@dataclass
class StorageSet:
    """五个物理基座 + 一个逻辑超图层。"""

    relational: RelationalRepository
    vector: VectorRepository
    graph: GraphRepository
    kv: KVRepository
    timeseries: TimeSeriesRepository
    hypergraph: HypergraphRepository

    def all(self) -> list[HealthCheckable]:
        return [
            self.relational,
            self.vector,
            self.graph,
            self.kv,
            self.timeseries,
            self.hypergraph,
        ]

    def close(self) -> None:
        # 先关超图（复用图谱基座），再关图谱，避免重复关闭同一 Database
        for engine in (self.hypergraph, self.graph, self.timeseries, self.kv, self.vector, self.relational):
            closer = getattr(engine, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001  关闭失败不阻断其他引擎
                    pass


@dataclass
class BrainApp:
    """运行时容器。所有依赖由本类显式持有，禁止模块级单例。"""

    config: BrainConfig
    storage: StorageSet
    embedder: Embedder
    reranker: Reranker
    llm: LocalLLM
    clock: HybridLogicalClock
    journal: NdjsonJournal
    event_store: EventStore
    hippocampus: Hippocampus
    prefrontal: Prefrontal
    life_loop: LifeLoop
    tier_manager: MemoryTierManager
    neocortex: Neocortex
    circadian: Circadian
    started_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def health_report(self) -> list[HealthStatus]:
        report = [engine.health() for engine in self.storage.all()]
        report.append(
            HealthStatus(
                engine="clock:hlc",
                ok=True,
                version=self.clock.device_id,
                detail=f"last={self.clock.last.to_token()}",
            )
        )
        return report

    def close(self) -> None:
        self.storage.close()


def _resolve_model_dir(config: BrainConfig, candidate: str) -> str | None:
    """模型既可能是本地目录，也可能是 HF 仓库名；只有本地存在才交给引擎。"""
    if not candidate:
        return None
    path = Path(candidate)
    if path.exists():
        return str(path)
    nested = config.model.llm_gguf_dir / candidate
    return str(nested) if nested.exists() else None


def build_storage(config: BrainConfig) -> StorageSet:
    """按 config 装配六个基座。顺序有依赖：图谱必须先于超图（共享 Database）。

    引擎开关：relational 是唯一事实源强制启用；其余可关（空实现占位，
    事件仍在 journal，重开后可重放/快照重建 —— 架构 §八）。
    """
    engines = config.storage.engines_enabled
    relational = SqliteRelationalDB(
        config.storage.sqlite_path,
        schema_version=config.sync.schema_version,
        busy_timeout_ms=config.storage.sqlite_busy_timeout_ms,
        wal_autocheckpoint=config.storage.wal_autocheckpoint,
    )
    vector: VectorRepository
    graph: GraphRepository
    if engines.get("vector", True):
        vector = LanceVectorDB(config.storage.lancedb_path, dense_dim=config.model.dense_dim)
    else:
        vector = DisabledVector()
    if engines.get("graph", True):
        graph = KuzuGraphDB(config.storage.kuzu_path)
    else:
        graph = DisabledGraph()
    kv: KVRepository = (
        LmdbKVStore(
            config.storage.lmdb_path,
            map_size_mb=config.storage.lmdb_map_size_mb,
            max_buckets=config.storage.lmdb_max_buckets,
        )
        if engines.get("kv", True)
        else DisabledKV()
    )
    timeseries: TimeSeriesRepository = (
        DuckDBTimeSeries(config.storage.duckdb_path, threads=config.storage.duckdb_threads)
        if engines.get("timeseries", True)
        else DisabledTimeSeries()
    )
    hypergraph: HypergraphRepository = (
        GraphHypergraph(graph)
        if engines.get("hypergraph", True) and engines.get("graph", True)
        else DisabledHypergraph()
    )
    return StorageSet(
        relational=relational,
        vector=vector,
        graph=graph,
        kv=kv,
        timeseries=timeseries,
        hypergraph=hypergraph,
    )


def _build_models(config: BrainConfig) -> tuple[Embedder, Reranker, LocalLLM]:
    """按每条通道（embed / llm）各自的 backend 与供应商装配。

    Embedding 与 LLM 可以是两家不同供应商（地址 / key 各自独立）。
    选了云端的通道：URL / 模型名任一缺失就退回本地实现并打印警告，
    保证"配置了一半"的设备也能启动 —— 不会因为云 API 抖动而拒绝开机。
    """
    model = config.model

    local_embed_dir = _resolve_model_dir(config, model.embedding_model)
    embedder: Embedder
    if model.embed_backend == "cloud":
        if model.embed_base_url and model.embed_model:
            from brain.cloud_models import CloudCredentials, CloudEmbedder, load_cloud_api_key

            credentials = CloudCredentials(
                model.embed_base_url,
                load_cloud_api_key(config, "embed"),
                timeout=model.embed_timeout_sec,
            )
            embedder = CloudEmbedder(credentials, model.embed_model, dense_dim=model.embed_dim)
        else:
            print("警告：Embedding 选了云端但 API 地址/模型名未配齐，退回本地/降级")
            embedder = BgeM3Embedder(
                dense_dim=model.dense_dim, model_dir=local_embed_dir, use_cuda=model.use_cuda
            )
    else:
        embedder = BgeM3Embedder(
            dense_dim=model.dense_dim, model_dir=local_embed_dir, use_cuda=model.use_cuda
        )

    local_llm_path = model.llm_gguf_dir / model.llm_gguf_file
    # ★ 本地 LLM 总开关：关掉时用 DisabledLLM 占位，裸写也绝不实例化 llama_cpp（规避 CUDA 崩）。
    #   认知契约下带 cognition 的写入走 from_agent，不经过 LLM，质量不受影响。
    if not model.llm_enabled:
        llm: LocalLLM = DisabledLLM()
    elif model.llm_backend == "cloud":
        if model.llm_base_url and model.llm_model:
            from brain.cloud_models import CloudCredentials, CloudLLM, load_cloud_api_key

            credentials = CloudCredentials(
                model.llm_base_url,
                load_cloud_api_key(config, "llm"),
                timeout=model.llm_timeout_sec,
            )
            llm = CloudLLM(credentials, model.llm_model)
        else:
            print("警告：LLM 选了云端但 API 地址/模型名未配齐，退回本地/规则")
            llm = QwenLocalLLM(
                model_path=str(local_llm_path) if local_llm_path.exists() else None,
                gpu_layers=model.llm_gpu_layers,
                max_tokens=model.llm_max_tokens,
            )
    else:
        llm = QwenLocalLLM(
            model_path=str(local_llm_path) if local_llm_path.exists() else None,
            gpu_layers=model.llm_gpu_layers,
            max_tokens=model.llm_max_tokens,
        )

    reranker = create_reranker(
        model_dir=_resolve_model_dir(config, config.model.reranker_model),
        use_cuda=config.model.use_cuda,
    )
    return embedder, reranker, llm


def build_app(config: BrainConfig | None = None, *, write_gate: Any | None = None) -> BrainApp:
    """组合根入口。三模型只构造不预热（懒加载），冷启动不碰磁盘上的大文件。

    W1（Q25）：传入共享 WriteGate 时，后台蒸馏与夜间巩固的写入也经闸门串行化；
    不传（测试 / 独立脚本）保持原来的直调语义。
    """
    config = config or get_config()
    storage = build_storage(config)
    embedder, reranker, llm = _build_models(config)
    clock = HybridLogicalClock(config.sync.device_id)

    # 同步平面写入端：本地事件段（fsync 后回执），Pull/Push 由 git_worker 负责
    journal = NdjsonJournal(
        config.sync.local_repo_dir,
        device_id=config.sync.device_id,
        segment_max_bytes=config.sync.segment_max_bytes,
        schema_version=config.sync.schema_version,
    )
    event_store = EventStore(config.sync.local_repo_dir, schema_version=config.sync.schema_version)

    # 认知层：全部只依赖 core.interfaces
    plasticity = SynapticPlasticity(config)
    amygdala = Amygdala(storage.kv, config)
    thalamus = Thalamus(storage.kv, config)
    extractor = RouterExtractor(llm, config)
    consolidator = MemoryConsolidator(
        llm=llm, relational=storage.relational, vector=storage.vector,
        embedder=embedder, config=config,
    )
    hippocampus = Hippocampus(
        relational=storage.relational,
        vector=storage.vector,
        graph=storage.graph,
        timeseries=storage.timeseries,
        hypergraph=storage.hypergraph,
        kv=storage.kv,
        clock=clock,
        extractor=extractor,
        thalamus=thalamus,
        amygdala=amygdala,
        plasticity=plasticity,
        embedder=embedder,
        event_log=journal,
        config=config,
        consolidator=consolidator,
    )
    prefrontal = Prefrontal(
        relational=storage.relational,
        vector=storage.vector,
        graph=storage.graph,
        kv=storage.kv,
        embedder=embedder,
        reranker=reranker,
        plasticity=plasticity,
        amygdala=amygdala,
        timeseries=storage.timeseries,
        config=config,
    )
    tier_manager = MemoryTierManager(
        storage.relational, plasticity, event_log=journal, config=config
    )
    neocortex = Neocortex(storage.relational, event_log=journal, config=config)

    # 项目档案实时提炼：agent 写入即异步进六引擎（事件驱动后台线程），夜间只兜底
    from workers.document_distiller import (
        start_distill_worker, enqueue_document,
    )
    from core.context_layers import set_distill_trigger
    from brain.write_gate import WriteRequest

    def _remember_via_gate(text: str, **kwargs: Any):
        if write_gate is None:
            return hippocampus.remember(text, **kwargs)
        return write_gate.submit(WriteRequest(
            fn=lambda: hippocampus.remember(text, **kwargs)
        )).result(timeout=30)

    _distill_save = lambda text, *, agent_id="distiller", namespace="document": (
        _remember_via_gate(text, agent_id=agent_id, namespace=namespace)
    )
    start_distill_worker(_distill_save)
    set_distill_trigger(enqueue_document)

    life_loop = LifeLoop(
        storage.timeseries,
        relational=storage.relational,
        graph=storage.graph,
        tier_manager=tier_manager,
        config=config,
        save_memory=_distill_save,
    )
    circadian = Circadian()

    def _nightly_consolidate() -> tuple[int, int, int]:
        consolidations = neocortex.consolidate()
        summaries = consolidator.summarize_clusters(
            consolidations,
            # summarize_clusters 可能带 agent_id 等关键字调用，透传避免 TypeError
            remember=lambda text, **kw: _remember_via_gate(
                text, **{"agent_id": "consolidator", **kw}
            ),
        )
        sweep_report = tier_manager.sweep()
        scanned = int(getattr(sweep_report, "scanned", 0) or 0)
        return len(consolidations), len(summaries), scanned

    Circadian.install_defaults(
        circadian,
        consolidate=_nightly_consolidate,
        decay_sweep=life_loop.forget_sweep,
        snapshot=lambda: None,       # git_worker/snapshot 接线在阶段 5
        morning_brief=life_loop.morning_brief,
    )

    return BrainApp(
        config=config,
        storage=storage,
        embedder=embedder,
        reranker=reranker,
        llm=llm,
        clock=clock,
        journal=journal,
        event_store=event_store,
        hippocampus=hippocampus,
        prefrontal=prefrontal,
        life_loop=life_loop,
        tier_manager=tier_manager,
        neocortex=neocortex,
        circadian=circadian,
        started_at_ms=int(time.time() * 1000),
    )


def self_check(config: BrainConfig | None = None, *, as_json: bool = False) -> int:
    """一键自检：冷启动耗时 + 六引擎健康 + 算力层降级状态。返回进程退出码。"""
    started = time.perf_counter()
    app = build_app(config)
    report = app.health_report()
    cold_start = time.perf_counter() - _PROCESS_START

    payload: dict[str, Any] = {
        "cold_start_sec": round(cold_start, 2),
        "cold_start_budget_sec": app.config.cold_start_timeout_sec,
        "cold_start_ok": cold_start < app.config.cold_start_timeout_sec,
        "device_id": app.config.sync.device_id,
        "engines": [status.as_dict() for status in report],
        "models": {
            "embedder": {
                "dense_dim": app.embedder.dense_dim,
                "degraded": app.embedder.degraded,
            },
            "reranker": {"degraded": app.reranker.degraded},
            "llm": {
                "gpu_layers": getattr(app.llm, "active_gpu_layers", None),
                "degraded": app.llm.degraded,
            },
        },
        "build_sec": round(time.perf_counter() - started, 2),
        "memories": {
            "neurons": app.storage.relational.count("neuron", active_only=False),
            "pad_points": app.storage.timeseries.count("pad_track"),
            "journal_last_hlc": _last_hlc(app.journal),
        },
    }
    failed = [status.engine for status in report if not status.ok]
    app.close()

    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"冷启动 {payload['cold_start_sec']}s / 预算 {payload['cold_start_budget_sec']}s "
              f"→ {'OK' if payload['cold_start_ok'] else '超时'}")
        print(f"{'引擎':<22}{'状态':<8}{'延迟ms':>9}  说明")
        for status in report:
            state = "OK" if status.ok else "FAIL"
            print(f"{status.engine:<22}{state:<8}{status.latency_ms:>9.2f}  {status.detail}")
        print(f"算力层：embedder(dim={app.embedder.dense_dim}, degraded={app.embedder.degraded}) "
              f"reranker(degraded={app.reranker.degraded}) llm(gpu_layers={payload['models']['llm']['gpu_layers']})")
    return 0 if not failed else 1


def gpu_runtime_info() -> dict[str, Any]:
    """只读探测：GPU 后端、CUDA 运行库、驱动版本（打包后也能用）。

    任何一项缺失都只记录、不抛；老机器由 provider 解析自动回落 CPU。
    """
    from engine.cuda_runtime import cuda_ready, cuda_runtime_status

    status = cuda_runtime_status()
    info: dict[str, Any] = {
        "llama_cpp_gpu_build": None,
        "cuda_driver_api": status["cuda_driver_api"],
        "cuda_dll_complete": status["dll_complete"],
        "cuda_driver_sufficient": status["driver_sufficient"],
        "cuda_dll_dirs": status["dll_dirs"],
        "cuda_ready": cuda_ready(),
    }
    try:
        from llama_cpp import llama_supports_gpu_offload

        info["llama_cpp_gpu_build"] = bool(llama_supports_gpu_offload())
    except Exception as exc:  # noqa: BLE001
        info["llama_cpp_error"] = f"{type(exc).__name__}: {exc}"
    return info


def probe_models(config: BrainConfig | None = None, *, as_json: bool = False) -> int:
    """主动加载三模型，实测 GPU 是否真的生效（显卡机验收用）。

    self_check 只构造对象：懒加载下 degraded 恒为 False、gpu_layers 是配置值，
    无法区分「CUDA 生效」与「根本没加载」。本函数触发真实加载并给出判定。
    """
    app = build_app(config)
    runtime = gpu_runtime_info()
    model_path = app.config.model.llm_gguf_dir / app.config.model.llm_gguf_file

    llm: dict[str, Any] = {
        "type": type(app.llm).__name__,
        "model_path": str(model_path),
        "model_exists": model_path.exists(),
        "requested_gpu_layers": app.config.model.llm_gpu_layers,
        **runtime,
    }
    loader = getattr(app.llm, "_ensure_loaded", None)
    try:
        if callable(loader):
            loader()
            llm["load"] = "ok"
        else:
            llm["load"] = "skipped: no loader"
    except Exception as exc:  # noqa: BLE001
        llm["load"] = f"error: {type(exc).__name__}: {exc}"
    llm["active_gpu_layers"] = getattr(app.llm, "active_gpu_layers", None)
    llm["degraded"] = app.llm.degraded
    llm["on_gpu"] = bool(
        runtime["llama_cpp_gpu_build"] is True
        and llm["load"] == "ok"
        and not llm["degraded"]
        and llm["active_gpu_layers"] != 0
    )

    embedder: dict[str, Any] = {"type": type(app.embedder).__name__}
    embed_loader = getattr(app.embedder, "_ensure_loaded", None)
    try:
        if callable(embed_loader):
            embed_loader()
            embedder["load"] = "ok"
        else:
            embedder["load"] = "skipped: no loader"
    except Exception as exc:  # noqa: BLE001
        embedder["load"] = f"error: {type(exc).__name__}: {exc}"
    embedder["providers"] = list(getattr(app.embedder, "providers", []))
    embedder["degraded"] = app.embedder.degraded
    embedder["on_gpu"] = "CUDAExecutionProvider" in embedder["providers"]

    embedder["load_error"] = getattr(app.embedder, "load_error", "")
    embedder["sparse_enabled"] = bool(getattr(app.embedder, "sparse_enabled", False))
    embedder["sparse_error"] = getattr(app.embedder, "sparse_error", "")
    embedder["sparse"] = (
        "ok: lexical head 已导出并与主模型共用 GPU provider"
        if embedder["sparse_enabled"]
        else f"unavailable: {embedder['sparse_error'] or '缺少 sparse_head.onnx'}"
    )

    reranker: dict[str, Any] = {"type": type(app.reranker).__name__}
    rerank_loader = getattr(app.reranker, "_ensure_loaded", None)
    try:
        if callable(rerank_loader):
            rerank_loader()
            reranker["load"] = "ok"
        else:
            reranker["load"] = "skipped: no loader"
    except Exception as exc:  # noqa: BLE001
        reranker["load"] = f"error: {type(exc).__name__}: {exc}"
    reranker["providers"] = list(getattr(app.reranker, "providers", []))
    reranker["degraded"] = app.reranker.degraded
    reranker["on_gpu"] = "CUDAExecutionProvider" in reranker["providers"]
    reranker["load_error"] = getattr(app.reranker, "load_error", "")

    all_on_gpu = bool(
        llm["on_gpu"] and embedder["on_gpu"] and reranker["on_gpu"]
        and embedder["sparse_enabled"]
    )
    if not llm["model_exists"]:
        verdict = "MODEL_MISSING"
    elif not llm["on_gpu"]:
        verdict = "CPU_ONLY" if runtime["llama_cpp_gpu_build"] is not True else "GPU_FELL_BACK_TO_CPU"
    elif all_on_gpu:
        verdict = "GPU_OK"
    else:
        verdict = "GPU_PARTIAL"
    hints = {
        "GPU_OK": "LLM / Embedding / Reranker 全部在 GPU，且 BGE-M3 lexical 稀疏头已生效",
        "GPU_PARTIAL": "有通道未生效：检查各通道 on_gpu / sparse_enabled 与 load 字段",
        "MODEL_MISSING": "把 Qwen3 GGUF 放到 model_path 指向的位置",
        "CPU_ONLY": "llama-cpp-python 是 CPU 构建或驱动过旧：需 CUDA 版 wheel + NVIDIA 驱动 >= 550",
        "GPU_FELL_BACK_TO_CPU": "已装 CUDA wheel 但加载时降级：检查驱动与 CUDA 运行时版本",
    }
    payload = {
        "verdict": verdict,
        "hint": hints[verdict],
        "all_on_gpu": all_on_gpu,
        "llm": llm,
        "embedder": embedder,
        "reranker": reranker,
    }
    app.close()

    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"判定：{verdict} —— {hints[verdict]}")
        for name, section in (("LLM", llm), ("Embedding", embedder), ("Reranker", reranker)):
            print(f"  {name}: " + " ".join(f"{k}={v}" for k, v in section.items()))
    return 0 if verdict == "GPU_OK" else 1


def serve_forever(config: BrainConfig | None = None) -> int:
    """一键启动：六引擎 + 认知层 + 网关 + 昼夜调度 + 同步 worker；Ctrl-C 优雅停。"""
    config = config or get_config()
    configure_logging(config.log_level)
    # W1：--serve 独立模式同样用统一闸门，避免留下第七条旁路写入路径
    from brain.write_gate import WriteGate

    write_gate = WriteGate()
    app = build_app(config, write_gate=write_gate)
    registry = AgentRegistry(app.storage.kv, timeseries=app.storage.timeseries, config=config)
    toolkit = MCPToolkit(
        registry=registry,
        hippocampus=app.hippocampus,
        prefrontal=app.prefrontal,
        relational=app.storage.relational,
        engines=app.storage.all(),
        config=config,
    )
    # 让 MCP 桥接令牌稳定：优先复用已持久化的 mcp_token.txt，否则生成并写回
    from core.runtime_paths import read_local_token, write_local_token
    import secrets as _secrets
    _bridge_token = read_local_token() or _secrets.token_urlsafe(24)
    if not read_local_token():
        write_local_token(_bridge_token)
    http = HTTPAdapter(registry=registry, toolkit=toolkit, config=config,
                       bridge_token=_bridge_token, write_gate=write_gate)
    projector = EventProjector(
        relational=app.storage.relational, vector=app.storage.vector, graph=app.storage.graph,
        hypergraph=app.storage.hypergraph, timeseries=app.storage.timeseries, kv=app.storage.kv,
        embedder=app.embedder,
    )
    worker = GitSyncWorker(
        root=config.sync.local_repo_dir, device_id=config.sync.device_id,
        event_store=app.event_store, projector=projector,
        remote=config.sync.remote_name, sync_interval_sec=config.sync.sync_interval_sec,
        write_debounce_sec=config.sync.write_debounce_sec,
    )
    snapshot_manager = SnapshotManager(config.sync.local_repo_dir, schema_version=config.sync.schema_version)
    app.circadian.register(
        "snapshot_export", hour=4, minute=5,
        callback=lambda: snapshot_manager.create(
            app.storage.relational,
            hlc_cutoff=(_last_hlc(app.journal) or "0:0:local"),
        ),
    )

    app.circadian.start()
    if config.sync.repo_url:
        worker.start()
    agent_id, token = registry.register("default-client")
    print(f"HTTP 适配器: http://127.0.0.1:{config.gateway.http_port}  (GET /health)")
    print(f"默认接入串: agent_id={agent_id} token={token}")
    if registry.ephemeral_secret:
        print("警告：未配置 GW_MCP_TOKEN_SECRET，令牌在进程重启后失效（生产必须设置）")
    print("Ctrl-C 优雅停止…")
    try:
        http.serve(host="127.0.0.1", port=config.gateway.http_port)
    except KeyboardInterrupt:
        pass
    finally:
        app.circadian.stop()
        worker.stop()
        app.journal.flush()
        app.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="超级大脑 v2.1 组合根")
    parser.add_argument("--self-check", action="store_true", help="只跑冷启动与六引擎自检后退出")
    parser.add_argument("--probe-models", action="store_true", help="主动加载三模型，实测 CUDA/GPU 是否生效")
    parser.add_argument("--serve", action="store_true", help="启动网关/调度/同步服务（阻塞）")
    parser.add_argument("--json", action="store_true", help="自检/模型探测输出 JSON")
    args = parser.parse_args(argv)

    if args.probe_models:
        return probe_models(as_json=args.json)
    if args.self_check:
        return self_check(as_json=args.json)
    if args.serve:
        return serve_forever()
    print("可用命令：python main.py --self-check | --probe-models | --serve | python scripts/verify_baseline.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
