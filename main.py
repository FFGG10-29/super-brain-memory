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
from engine.embedding import BgeM3Embedder
from engine.local_llm import QwenLocalLLM
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

__all__ = ["BrainApp", "StorageSet", "build_app", "build_storage", "self_check"]

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

    local_llm_path = model.llm_gguf_dir / model.llm_model
    llm: LocalLLM
    if model.llm_backend == "cloud":
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
            )
    else:
        llm = QwenLocalLLM(
            model_path=str(local_llm_path) if local_llm_path.exists() else None,
            gpu_layers=model.llm_gpu_layers,
        )

    reranker = create_reranker(
        model_dir=_resolve_model_dir(config, config.model.reranker_model),
        use_cuda=config.model.use_cuda,
    )
    return embedder, reranker, llm


def build_app(config: BrainConfig | None = None) -> BrainApp:
    """组合根入口。三模型只构造不预热（懒加载），冷启动不碰磁盘上的大文件。"""
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
    life_loop = LifeLoop(
        storage.timeseries,
        relational=storage.relational,
        graph=storage.graph,
        tier_manager=tier_manager,
        config=config,
    )
    circadian = Circadian()
    Circadian.install_defaults(
        circadian,
        consolidate=lambda: (len(neocortex.consolidate()), tier_manager.sweep()),
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


def serve_forever(config: BrainConfig | None = None) -> int:
    """一键启动：六引擎 + 认知层 + 网关 + 昼夜调度 + 同步 worker；Ctrl-C 优雅停。"""
    config = config or get_config()
    configure_logging(config.log_level)
    app = build_app(config)
    registry = AgentRegistry(app.storage.kv, timeseries=app.storage.timeseries, config=config)
    toolkit = MCPToolkit(
        registry=registry,
        hippocampus=app.hippocampus,
        prefrontal=app.prefrontal,
        relational=app.storage.relational,
        engines=app.storage.all(),
        config=config,
    )
    http = HTTPAdapter(registry=registry, toolkit=toolkit, config=config)
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
    parser.add_argument("--serve", action="store_true", help="启动网关/调度/同步服务（阻塞）")
    parser.add_argument("--json", action="store_true", help="自检输出 JSON")
    args = parser.parse_args(argv)

    if args.self_check:
        return self_check(as_json=args.json)
    if args.serve:
        return serve_forever()
    print("可用命令：python main.py --self-check | python main.py --serve | python scripts/verify_baseline.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
