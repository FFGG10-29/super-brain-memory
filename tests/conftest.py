"""tests.conftest —— 全局夹具：临时数据目录、隔离配置、六引擎实例、认知层装配。

原则：测试绝不写 data/，全部重定向到 tmp_path；配置显式构造，不读环境变量与 .env。
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

# 任何测试都不许污染真实用户配置（%APPDATA%/super-brain）——必须在 import config 前设置
os.environ["SUPER_BRAIN_CONFIG_DIR"] = tempfile.mkdtemp(prefix="brain_test_config_")
# 号池移植后的隔离：.env 的 POOL_* 与本机真实凭证（加密态已可解密）绝不泄进测试。
# 环境变量优先级高于 .env（pydantic-settings），在这里压死。
os.environ["POOL_ENABLED"] = "false"
os.environ["POOL_FREE_LANE"] = "false"
os.environ["XDPOOL_ACCOUNTS_FILE"] = os.path.join(
    tempfile.gettempdir(), "brain_test_no_xdpool_accounts.json")
os.environ["WORKBUDDY_AUTH_FILE"] = os.path.join(
    tempfile.gettempdir(), "brain_test_no_auth.info")

from brain.amygdala import Amygdala
from brain.hippocampus import Hippocampus
from brain.prefrontal import Prefrontal
from brain.router_extractor import RouterExtractor
from brain.synaptic_plasticity import SynapticPlasticity
from brain.thalamus import Thalamus
from config import BrainConfig, ModelConfig, StorageConfig, SyncConfig
from core.ids import HybridLogicalClock
from engine.reranker import IdentityReranker
from main import BrainApp, build_app, build_storage
from sync.event_store import EventStore
from sync.journal import NdjsonJournal
from tests.fakes import FakeEmbedder, FakeLLM

DENSE_DIM = 8  # 测试用小维度：够验证 fixed_size_list 校验，又不拖慢用例


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path, monkeypatch):
    """每个用例一套独立的用户配置目录。

    设置页「保存」现在会真正落盘（含 storage 路径），若共用一个配置目录，
    上一个用例保存的临时库路径会被下一个用例的后端读回，
    直接开在别人的 tmp_path 上 → Kùzu 单写锁冲突（曾导致成片 ERROR）。
    """
    monkeypatch.setenv("SUPER_BRAIN_CONFIG_DIR", str(tmp_path / "_user_config"))
    # 用户目录也要隔离：MCP 桥接描述文件（mcp_bridge.json）在 user_dir() 下，
    # 不隔离会把真实桌面进程的桥接地址读进测试
    monkeypatch.setenv("SUPER_BRAIN_HOME", str(tmp_path / "_user_home"))
    # 原先的 clear_overrides()（桌面用户配置覆盖）已随配置机制重构退役。
    yield


@pytest.fixture(autouse=True)
def _isolate_user_config_state(monkeypatch):
    """（已退役）原先隔离用户配置，防止测试误读真实仓库地址与 PAT 去 git push。

    配置统一走 core.user_config + 显式构造的 BrainConfig；
    保留空壳以兼容 autouse 语义。
    """
    yield


@pytest.fixture()
def brain_config(tmp_path) -> BrainConfig:
    return BrainConfig(
        storage=StorageConfig(
            sqlite_path=tmp_path / "relational.db",
            lancedb_path=tmp_path / "vector.lance",
            kuzu_path=tmp_path / "graph.kuzu",
            lmdb_path=tmp_path / "kv.lmdb",
            duckdb_path=tmp_path / "timeseries.db",
        ),
        model=ModelConfig(dense_dim=DENSE_DIM, use_cuda=False, llm_gguf_dir=tmp_path / "models"),
        sync=SyncConfig(device_id="dev-test", local_repo_dir=tmp_path / "brain-sync"),
    )


@pytest.fixture()
def storage(brain_config: BrainConfig):
    engines = build_storage(brain_config)
    try:
        yield engines
    finally:
        engines.close()


@pytest.fixture()
def app(brain_config: BrainConfig) -> Iterator[BrainApp]:
    instance = build_app(brain_config)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def brain_factory(storage, brain_config):
    """按需装配认知层：可选 LLM 脚本与 embedder（默认无 embedder = 纯本地文本通道）。"""
    instances: list[object] = []

    def build(*, llm_payload=None, with_embedder: bool = False, with_journal: bool = False):
        clock = HybridLogicalClock(brain_config.sync.device_id)
        embedder = FakeEmbedder(brain_config.model.dense_dim) if with_embedder else None
        if with_journal:
            journal = NdjsonJournal(
                brain_config.sync.local_repo_dir,
                device_id=brain_config.sync.device_id,
                segment_max_bytes=brain_config.sync.segment_max_bytes,
                schema_version=brain_config.sync.schema_version,
            )
            event_store = EventStore(brain_config.sync.local_repo_dir, schema_version=brain_config.sync.schema_version)
        else:
            journal, event_store = None, None
        plasticity = SynapticPlasticity(brain_config)
        amygdala = Amygdala(storage.kv, brain_config)
        thalamus = Thalamus(storage.kv, brain_config)
        extractor = RouterExtractor(FakeLLM(llm_payload), brain_config)
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
            config=brain_config,
        )
        prefrontal = Prefrontal(
            relational=storage.relational,
            vector=storage.vector,
            graph=storage.graph,
            kv=storage.kv,
            embedder=embedder,
            reranker=IdentityReranker(),
            plasticity=plasticity,
            amygdala=amygdala,
            timeseries=storage.timeseries,
            config=brain_config,
        )
        bundle = SimpleNamespace(
            clock=clock,
            embedder=embedder,
            extractor=extractor,
            thalamus=thalamus,
            amygdala=amygdala,
            plasticity=plasticity,
            hippocampus=hippocampus,
            prefrontal=prefrontal,
            journal=journal,
            event_store=event_store,
            storage=storage,
            config=brain_config,
        )
        instances.append(bundle)
        return bundle

    return build
