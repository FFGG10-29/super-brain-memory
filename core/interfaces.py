"""core.interfaces —— 依赖倒置唯一出口（全部 ABC）。

依赖方向：brain/ → core.interfaces ← storage/ engine/
cognitive 层禁止 import 任何具体引擎；storage 层不得反向 import models（契约是 Mapping 记录）。

契约选择（见 .memory/DECISIONS.md ADR-002）：
    存储层收发 `Record = Mapping[str, Any]`，Pydantic 实体只活在 models/ 与 brain/。
    理由：投影器重放事件手里就是 JSON 字典，往返实体化是纯损耗。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from core.ids import HLCStamp

__all__ = [
    "EntityKind",
    "EventType",
    "Embedding",
    "Embedder",
    "EventLog",
    "GraphRepository",
    "HealthCheckable",
    "HealthStatus",
    "HLCStamp",
    "HypergraphRepository",
    "KVRepository",
    "LocalLLM",
    "MemoryType",
    "Namespace",
    "OutboxItem",
    "Projector",
    "Pyramid",
    "RecallFilter",
    "Record",
    "RelationalRepository",
    "Reranker",
    "ScoredId",
    "Tier",
    "TimeSeriesRepository",
    "TimeSeriesTable",
    "VectorRecord",
    "VectorRepository",
]

# ── 领域字面量（与 tunerstudio/tunerstudio 无关，纯粹是记忆域枚举）──────────
Record = Mapping[str, Any]
EntityKind = Literal["neuron", "synapse", "hyperedge"]
MemoryType = Literal["fact", "code", "scenario", "emotion", "profile"]
# 命名空间放宽为 str：历史值 private/group/global 保持兼容，新增 topic:<空间id>
# 用作记忆分区（A 包类别索引）。分区只改这一列，不改六引擎投影链、不碰 Kùzu 单写锁。
Namespace = str
Tier = Literal["buffer", "core", "recall", "archive"]
Pyramid = Literal["raw", "fact", "topic", "worldview"]
EventType = Literal[
    "ingest",             # 写入
    "supersede",          # 冲突失效
    "reinforce",          # 召回强化
    "tier_change",        # 晋升/降级
    "consolidate",        # 金字塔升维 / 画像更新
    "policy",             # 策略变更
    "tombstone",          # 合规级删除标记
    "overwrite",          # W2：事务性内容覆盖式更新（只记最终态，带新 payload）
    "media_interaction",  # W2：媒体交互 meta（不产生 neuron）
    "reconsolidate",      # E5/Q32：再巩固窗口内原地合并（id 不变 + payload 原地更新，必须产生事件）
]
TimeSeriesTable = Literal["pad_track", "access_log", "decay_event", "consolidation_failures"]


# ── 通用值对象 ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class HealthStatus:
    """健康检查结果。看板与启动自检共用同一结构。"""

    engine: str
    ok: bool
    latency_ms: float = 0.0
    version: str = ""
    detail: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 2),
            "version": self.version,
            "detail": self.detail,
            **dict(self.extra),
        }


@dataclass(frozen=True)
class ScoredId:
    """召回结果的最小单元：id + 分数 + 通道名（RRF 需要通道名做归并）。"""

    id: str
    score: float
    channel: str = ""


@dataclass(frozen=True)
class Embedding:
    dense: Sequence[float]
    sparse: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class VectorRecord:
    """向量库的一行：向量 + 正文 + 预过滤用标量列。"""

    id: str
    dense: Sequence[float]
    text: str
    sparse: Mapping[str, float] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecallFilter:
    """预过滤条件：先标量过滤，再向量检索（架构 §二 裁决 5）。"""

    namespaces: tuple[str, ...] = ()
    tiers: tuple[str, ...] = ()
    memory_types: tuple[str, ...] = ()
    active_only: bool = True


@dataclass(frozen=True)
class OutboxItem:
    """六库分发的一次投递任务。"""

    outbox_id: int
    event_id: str
    target: str
    payload: Record
    attempts: int = 0


# ── 基类 ───────────────────────────────────────────────────────────────────
class HealthCheckable(ABC):
    """所有嵌入式基座的统一探针接口。"""

    name: str = "unknown"

    @abstractmethod
    def health(self) -> HealthStatus:
        """返回连通性/延迟/版本；实现内部不得抛异常（失败要转成 ok=False）。"""

    def close(self) -> None:  # noqa: B027  允许默认空实现：无状态适配器无需释放
        """释放连接（幂等，可重复调用）。"""


# ── 三模型（算力层，可整体替换为远端 API）─────────────────────────────────
class Embedder(ABC):
    """BGE-M3：一次编码同时产出稠密 + 稀疏。"""

    @property
    @abstractmethod
    def dense_dim(self) -> int: ...

    @property
    def degraded(self) -> bool:
        """True 表示已从 CUDA 降到 CPU 或用了替代实现。"""
        return False

    @abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> list[Embedding]: ...

    def embed_query(self, text: str) -> Embedding:
        return self.embed_documents([text])[0]


class Reranker(ABC):
    """bge-reranker-v2-m3：交叉编码精排。"""

    @property
    def degraded(self) -> bool:
        return False

    @abstractmethod
    def rerank(
        self,
        query: str,
        candidates: Sequence[tuple[str, str]],
        *,
        top_k: int | None = None,
    ) -> list[ScoredId]:
        """candidates 为 (id, text) 序列；返回按分数降序的 ScoredId。"""


class LocalLLM(ABC):
    """Qwen3-0.6B GGUF：强制 JSON 输出。"""

    @property
    def degraded(self) -> bool:
        return False

    @abstractmethod
    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        required_keys: Sequence[str] = (),
        max_retries: int = 2,
    ) -> Mapping[str, Any]:
        """返回已通过 JSON 解析（与 required_keys 校验）的对象；失败抛 EngineUnavailable。"""

    def complete_text(self, system_prompt: str, user_prompt: str) -> str:
        """W6 / Q15：自由文本生成（叙事重构、Ollama 不可达时的 GGUF 兜底）。

        非抽象方法：现有 FakeLLM / DisabledLLM 不实现也能正常工作，只是显式抛不可用。
        """
        from core.errors import EngineUnavailable

        raise EngineUnavailable("当前 LocalLLM 未实现自由文本生成")


# ── 六引擎仓储 ─────────────────────────────────────────────────────────────
class RelationalRepository(HealthCheckable, ABC):
    """SQLite(WAL)：唯一事实源 + 双时态 + outbox + FTS5。"""

    # 实体读写
    @abstractmethod
    def upsert(self, kind: EntityKind, record: Record) -> None: ...

    @abstractmethod
    def get(self, kind: EntityKind, entity_id: str) -> Record | None: ...

    @abstractmethod
    def deactivate(
        self,
        kind: EntityKind,
        entity_id: str,
        *,
        valid_to_ms: int,
        tx_to: HLCStamp,
    ) -> None:
        """封存而非删除：valid_to/tx_to 打点 + is_active=0（架构 §七 冲突消解）。"""

    @abstractmethod
    def set_tier(self, kind: EntityKind, entity_id: str, tier: Tier) -> None: ...

    @abstractmethod
    def query(
        self,
        kind: EntityKind,
        *,
        namespaces: Sequence[str] = (),
        tiers: Sequence[str] = (),
        memory_types: Sequence[str] = (),
        active_only: bool = True,
        limit: int = 50,
        offset: int = 0,
        since_ms: int | None = None,   # W4：按时间窗过滤（hyperedge 补边用），None=不过滤
    ) -> list[Record]: ...

    @abstractmethod
    def full_text_search(self, kind: EntityKind, query: str, *, limit: int = 20) -> list[Record]: ...

    @abstractmethod
    def count(self, kind: EntityKind, *, active_only: bool = True) -> int: ...

    # outbox：六库幂等分发
    @abstractmethod
    def outbox_enqueue(self, event_id: str, target: str, payload: Record) -> None: ...

    @abstractmethod
    def outbox_claim(self, limit: int = 100) -> list[OutboxItem]: ...

    @abstractmethod
    def outbox_ack(self, outbox_ids: Sequence[int]) -> None: ...

    @abstractmethod
    def outbox_fail(self, outbox_id: int, error: str, *, max_attempts: int = 3) -> None: ...

    @abstractmethod
    def outbox_pending_count(self) -> int: ...

    # 重放幂等：事件是否首次出现
    @abstractmethod
    def mark_event_applied(self, event_id: str) -> bool:
        """返回 True 表示首次（可以应用），False 表示重复（必须跳过）。"""


class VectorRepository(HealthCheckable, ABC):
    """LanceDB：稠密 + 稀疏 + 全文三通道，一律预过滤 ANN。"""

    @property
    @abstractmethod
    def dense_dim(self) -> int: ...

    @abstractmethod
    def upsert(self, records: Sequence[VectorRecord]) -> None: ...

    @abstractmethod
    def delete(self, ids: Sequence[str]) -> None: ...

    @abstractmethod
    def search_dense(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        where: RecallFilter | None = None,
    ) -> list[ScoredId]: ...

    @abstractmethod
    def search_sparse(
        self,
        weights: Mapping[str, float],
        *,
        limit: int,
        where: RecallFilter | None = None,
    ) -> list[ScoredId]: ...

    @abstractmethod
    def search_text(
        self,
        query: str,
        *,
        limit: int,
        where: RecallFilter | None = None,
    ) -> list[ScoredId]: ...

    @abstractmethod
    def count(self) -> int: ...


class GraphRepository(HealthCheckable, ABC):
    """Kùzu：实体图谱 + 超边二部图编码。"""

    @abstractmethod
    def upsert_entity(
        self,
        entity_id: str,
        *,
        name: str,
        entity_type: str = "concept",
        props: Mapping[str, Any] | None = None,
    ) -> None: ...

    @abstractmethod
    def upsert_hyperedge_node(
        self,
        hyperedge_id: str,
        *,
        label: str,
        occurred_at_ms: int,
    ) -> None: ...

    @abstractmethod
    def add_relation(
        self,
        subject_id: str,
        predicate: str,
        object_id: str,
        *,
        weight: float = 1.0,
        tx_from: HLCStamp | None = None,
    ) -> None: ...

    @abstractmethod
    def add_member(self, hyperedge_id: str, entity_id: str, *, weight: float = 1.0) -> None: ...

    @abstractmethod
    def supersede(self, new_id: str, old_id: str, *, tx_from: HLCStamp) -> None: ...

    def supersede_edges_of(self, neuron_id: str, *, limit: int = 20) -> list[dict[str, str]]:
        """E2：读取以该 neuron 为一端的 SUPERSEDES 边（对端 id）。

        非抽象默认实现：返回空列表（表示"无痕迹"）。具体实现可选覆写——
        KuzuGraphDB 已覆写；无法读边的实现静默返回空，调用方按"无冲突"处理。
        """
        return []

    @abstractmethod
    def neighbors(self, entity_id: str, *, hops: int = 1, limit: int = 20) -> list[Record]: ...

    @abstractmethod
    def members(self, hyperedge_id: str) -> list[str]: ...

    @abstractmethod
    def hyperedges_of(self, entity_id: str, *, limit: int = 20) -> list[str]: ...

    @abstractmethod
    def count_nodes(self, label: str = "Entity") -> int: ...

    @abstractmethod
    def count_edges(self, rel: str = "RELATES") -> int: ...


class KVRepository(HealthCheckable, ABC):
    """LMDB：画像槽位 / 偏好 / 精确键直读 / 限流计数。"""

    @abstractmethod
    def put(self, bucket: str, key: str, value: bytes) -> None: ...

    @abstractmethod
    def get(self, bucket: str, key: str) -> bytes | None: ...

    @abstractmethod
    def delete(self, bucket: str, key: str) -> None: ...

    @abstractmethod
    def scan_prefix(self, bucket: str, prefix: str, *, limit: int = 100) -> list[tuple[str, bytes]]: ...

    @abstractmethod
    def incr(self, bucket: str, key: str, *, ttl_ms: int | None = None) -> int: ...

    # 便捷方法：JSON 编解码（所有实现共享，不是每个引擎各自实现一遍）
    def put_json(self, bucket: str, key: str, value: Mapping[str, Any] | Sequence[Any]) -> None:
        import json

        self.put(bucket, key, json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))

    def get_json(self, bucket: str, key: str) -> Any | None:
        import json

        raw = self.get(bucket, key)
        return None if raw is None else json.loads(raw.decode("utf-8"))


class TimeSeriesRepository(HealthCheckable, ABC):
    """DuckDB：PAD 轨迹 / 访问审计 / 衰减事件，只写不改。"""

    @abstractmethod
    def append(self, table: TimeSeriesTable, rows: Sequence[Record]) -> None: ...

    @abstractmethod
    def fetch(
        self,
        table: TimeSeriesTable,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
        entity_id: str | None = None,
        limit: int = 1000,
    ) -> list[Record]: ...

    @abstractmethod
    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[Record]:
        """只读分析入口（夜间再拟合、命中率统计）。实现侧应拒绝写语句。"""

    @abstractmethod
    def count(self, table: TimeSeriesTable) -> int: ...


class HypergraphRepository(HealthCheckable, ABC):
    """超图逻辑层：物理编码进图谱，高阶分析（HyperNetX）在内存做。"""

    @abstractmethod
    def add(self, hyperedge: Record) -> None: ...

    @abstractmethod
    def members(self, hyperedge_id: str) -> list[str]: ...

    @abstractmethod
    def hyperedges_of(self, entity_id: str, *, limit: int = 20) -> list[str]: ...

    @abstractmethod
    def incidence(self, hyperedge_ids: Sequence[str] | None = None) -> Mapping[str, list[str]]: ...

    def s_connected_components(self, hyperedge_ids: Sequence[str] | None = None) -> list[list[str]]:
        """s-连通分量；实现可降级为普通连通（无 hypernetx 时）。"""
        raise NotImplementedError


# ── 事件日志 / 投影 ────────────────────────────────────────────────────────
class EventLog(ABC):
    """追加式 NDJSON 事件日志（同步平面的写入端）。"""

    @abstractmethod
    def append(self, event: Record) -> None:
        """返回即代表已 fsync 落盘（架构 §8.1：先落盘再回执）。"""

    @abstractmethod
    def iter_events(self, *, after: HLCStamp | None = None) -> Iterator[Record]: ...

    @abstractmethod
    def last_stamp(self) -> HLCStamp | None: ...

    def flush(self) -> None:  # noqa: B027  默认无缓冲
        """强制刷盘（无缓冲实现可为空）。"""


class Projector(ABC):
    """事件 → 六引擎的确定性投影。"""

    @abstractmethod
    def apply(self, event: Record) -> None:
        """幂等应用单条事件；重复事件不得产生副作用。"""

    @abstractmethod
    def rebuild_all(self, events: Iterable[Record]) -> None:
        """全量重建（新设备冷启动 / 快照重建兜底）。"""
