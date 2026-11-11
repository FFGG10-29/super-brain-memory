"""workers.life_loop —— 默认模式网络：夜间复盘、图谱修剪、遗忘清扫、晨报。

纪律：只读时序库/主库，改写必须走海马体或 MemoryTierManager 发事件；
      不物理删除任何记录（归档与弱边标记都保留可追溯性）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from config import BrainConfig, get_config
from core.interfaces import EventLog, GraphRepository, RelationalRepository, TimeSeriesRepository
from brain.memory_tier_manager import MemoryTierManager, TierDecision
from brain.synaptic_plasticity import SynapticPlasticity
from models.neuron import Neuron
from workers.document_distiller import distill_documents as _distill_documents

__all__ = ["EmotionalReview", "LifeLoop", "NightlyReport"]

_MS_PER_DAY = 86_400_000.0


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class EmotionalReview:
    samples: int
    mean_p: float
    mean_a: float
    mean_d: float
    trend: str
    delta_p: float
    anomalies: list[dict[str, Any]] = field(default_factory=list)

    def as_brief(self) -> str:
        if self.samples == 0:
            return "情绪复盘：无样本（当天没有写入）"
        trend_text = {
            "improving": "情绪回升",
            "worsening": "情绪走低",
            "stable": "情绪平稳",
            "insufficient": "样本不足",
        }.get(self.trend, self.trend)
        lines = [
            f"情绪复盘：{self.samples} 条样本，均值 P{self.mean_p:+.2f}/A{self.mean_a:+.2f}/D{self.mean_d:+.2f} → {trend_text}",
        ]
        if self.anomalies:
            lines.append(f"异常波动 {len(self.anomalies)} 处，已标记待复查")
        return "\n".join(lines)


@dataclass
class NightlyReport:
    emotional: EmotionalReview | None = None
    archived: list[str] = field(default_factory=list)
    tier_changes: int = 0
    weak_relations: list[str] = field(default_factory=list)
    consolidations: int = 0
    distilled_docs: int = 0
    neurons_written: int = 0

    def as_brief(self) -> str:
        lines = ["【晨报】"]
        if self.emotional:
            lines.append(self.emotional.as_brief())
        lines.append(f"分层迁移 {self.tier_changes} 条，归档 {len(self.archived)} 条")
        lines.append(f"金字塔固化 {self.consolidations} 个主题，弱关系标记 {len(self.weak_relations)} 条")
        if self.distilled_docs:
            lines.append(f"档案自动提炼 {self.distilled_docs} 篇 → 写入神经元 {self.neurons_written} 条")
        return "\n".join(lines)


class LifeLoop:
    def __init__(
        self,
        timeseries: TimeSeriesRepository,
        *,
        relational: RelationalRepository | None = None,
        graph: GraphRepository | None = None,
        tier_manager: MemoryTierManager | None = None,
        config: BrainConfig | None = None,
        save_memory: Callable[..., Any] | None = None,
        event_log: EventLog | None = None,
        plasticity: SynapticPlasticity | None = None,
    ) -> None:
        self._timeseries = timeseries
        self._relational = relational
        self._graph = graph
        self._tier_manager = tier_manager
        self._config = config or get_config()
        self._save_memory = save_memory
        self._event_log = event_log      # E3：读 supersede 事件统计被纠正次数
        self._plasticity = plasticity or SynapticPlasticity(config=self._config)

    def emotional_review(self, *, since_ms: int | None = None, agent_id: str | None = None) -> EmotionalReview:
        since = _now_ms() - int(_MS_PER_DAY * 1000) if since_ms is None else int(since_ms)
        where = "WHERE ts_ms >= ?"
        params: list[object] = [since]
        if agent_id:
            where += " AND agent_id = ?"
            params.append(agent_id)
        rows = self._timeseries.query(
            f"SELECT ts_ms, p, a, d FROM pad_track {where} ORDER BY ts_ms", params
        )
        if not rows:
            return EmotionalReview(0, 0.0, 0.0, 0.0, "insufficient", 0.0)

        count = len(rows)
        mean_p = sum(float(row["p"]) for row in rows) / count
        mean_a = sum(float(row["a"]) for row in rows) / count
        mean_d = sum(float(row["d"]) for row in rows) / count
        half = max(1, count // 2)
        first_half, second_half = rows[:half], (rows[half:] or rows[-1:])
        delta_p = (
            sum(float(row["p"]) for row in second_half) / len(second_half)
            - sum(float(row["p"]) for row in first_half) / len(first_half)
        )
        if count < 2:
            trend = "insufficient"
        elif delta_p > 0.15:
            trend = "improving"
        elif delta_p < -0.15:
            trend = "worsening"
        else:
            trend = "stable"
        anomalies = [
            {
                "ts_ms": int(row["ts_ms"]),
                "p": float(row["p"]),
                "a": float(row["a"]),
                "reason": "高负唤醒",
            }
            for row in rows
            if float(row["p"]) <= self._config.amygdala.negative_valence_threshold
            and float(row["a"]) >= self._config.amygdala.negative_arousal_threshold
        ]
        return EmotionalReview(count, mean_p, mean_a, mean_d, trend, delta_p, anomalies[:20])

    def forget_sweep(self, *, limit: int = 500) -> list[str]:
        """衰减清扫：由分层管理器按可提取性归档（不物理删除）。"""
        if self._tier_manager is None:
            return []
        report = self._tier_manager.sweep(limit=limit)
        return report.archived

    # ── E3 / 主动遗忘：预测价值扫描 ────────────────────────────────────────
    def _supersede_counts(self) -> dict[str, int]:
        """从事件日志统计每条记忆的"被纠正"次数。

        supersede 事件 payload 带 old_id/new_id（brain/memory_editor.py 发出）：
        old_id = 被推翻的旧记忆（直接纠正）；new_id = 作为纠正产物落地的记忆
        （血统上的不稳定性）。两者都计入该 id 的纠正计数。
        事件日志不可用或缺席时返回空表（corrections 记 0，不阻断扫描）。
        """
        counts: dict[str, int] = {}
        if self._event_log is None:
            return counts
        try:
            for event in self._event_log.iter_events():
                if str(event.get("type") or "") != "supersede":
                    continue
                payload = event.get("payload") or {}
                for field_name in ("old_id", "new_id"):
                    neuron_id = str(payload.get(field_name) or "")
                    if neuron_id:
                        counts[neuron_id] = counts.get(neuron_id, 0) + 1
        except Exception:  # noqa: BLE001  事件日志读失败不阻断遗忘扫描
            return {}
        return counts

    def predictive_value_sweep(self, *, now_ms: int | None = None) -> dict[str, Any]:
        """E3：对 active 的 fact/scenario 记忆逐条计算「预测价值」并按判据处置。

        信号构成（config.forget 可调）：
        - 可提取性：FSRS retrievability（复用 SynapticPlasticity，时间衰减主信号）；
        - 使用度：access_log.neuron_ids（E3）反序列化的 30 天精确召回次数平滑
          归一化为主信号；旧格式行无 neuron_ids 时回退 neuron.recall_count
          （access_log 行数仍作为总量趋势放进结果）；
        - 信任度：1 - 被纠正度（supersede 事件计数，血统越不稳定分越低）。
        评分 = w_retention*retention + w_usage*usage + w_trust*(1-correction)，[0,1]。

        处置（复用 tier_manager 的降级机制与 tier_change 事件，不另起第二套）：
        - 低价值 + 低 recall_count → set_tier("archive") + tier_manager._emit 事件；
        - 高价值 + 低访问 → 保持 recall 层，列入 review 供人审；
        - core 层不动（分层不变量：核心层不自动降级）。
        """
        cfg = self._config.forget
        now = _now_ms() if now_ms is None else int(now_ms)
        result: dict[str, Any] = {
            "scanned": 0, "archived": [], "review": [], "events_emitted": 0,
            "access_log_rows_30d": 0, "enabled": bool(cfg.enabled),
        }
        if not cfg.enabled or self._tier_manager is None or self._relational is None:
            result["skipped"] = "forget disabled or relational/tier_manager missing"
            return result

        # 近 30 天 access_log：整体趋势 + E3 精确归因（neuron_ids 反序列化，
        # 统计每条记忆的精确召回次数）。旧格式行无 neuron_ids（None/空）→ 跳过不炸。
        precise_hits: dict[str, int] = {}
        try:
            since = now - int(_MS_PER_DAY * cfg.window_days)
            rows = self._timeseries.fetch("access_log", since_ms=since, limit=100000)
            result["access_log_rows_30d"] = len(rows)
            for row in rows:
                raw = row.get("neuron_ids")
                if not raw:
                    continue
                try:
                    ids = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                except (TypeError, ValueError):
                    continue
                if isinstance(ids, str):
                    ids = [ids]
                if not isinstance(ids, (list, tuple)):
                    continue
                for neuron_id in ids:
                    neuron_id = str(neuron_id or "")
                    if neuron_id:
                        precise_hits[neuron_id] = precise_hits.get(neuron_id, 0) + 1
        except Exception:  # noqa: BLE001  时序库不可用时跳过趋势项
            pass
        result["recall_hits_30d"] = precise_hits   # 每条记忆的 30 天精确召回次数

        corrections = self._supersede_counts()
        records = self._relational.query(
            "neuron", memory_types=tuple(cfg.memory_types), active_only=True,
            limit=int(cfg.scan_limit),
        )
        for record in records:
            neuron = Neuron.from_record(record)
            if neuron.tier not in ("buffer", "recall"):
                continue  # core 不自动降级；archive 不重复处置
            result["scanned"] += 1
            retention = self._plasticity.retrievability(neuron, now_ms=now)
            hits_30d = precise_hits.get(neuron.uuid7_id, 0)
            if hits_30d > 0:
                usage = hits_30d / (hits_30d + 5.0)   # E3：30 天精确召回平滑归一化
            else:
                usage = neuron.recall_count / (neuron.recall_count + 5.0)   # 回退（原行为）
            correction = min(1.0, corrections.get(neuron.uuid7_id, 0) / 3.0)
            value = min(1.0, max(0.0, (
                cfg.w_retention * retention
                + cfg.w_usage * usage
                + cfg.w_trust * (1.0 - correction)
            )))
            if value <= cfg.archive_value_threshold \
                    and neuron.recall_count <= cfg.max_recall_count_archive:
                self._relational.set_tier("neuron", neuron.uuid7_id, "archive")
                decision = TierDecision(
                    neuron.uuid7_id, neuron.tier, "archive",
                    f"预测价值 {value:.2f} 低于归档阈值 "
                    f"(retention={retention:.2f}, recall_count={neuron.recall_count})",
                )
                if self._tier_manager._emit(neuron, decision, now):  # noqa: SLF001  复用既有事件机制
                    result["events_emitted"] += 1
                result["archived"].append(neuron.uuid7_id)
            elif value >= cfg.review_value_threshold \
                    and neuron.recall_count <= cfg.review_max_recall_count:
                result["review"].append({
                    "id": neuron.uuid7_id, "value": round(value, 4),
                    "recall_count": neuron.recall_count, "tier": neuron.tier,
                    "recall_hits_30d": hits_30d,
                })
        return result

    def graph_prune(self, *, limit: int = 200) -> list[str]:
        """图谱修剪：找出无邻居的弱关系主体，输出待修剪清单（不删边，供复盘确认）。

        Kùzu 当前接口不暴露删除，且事件日志是全量重建真源 —— 直接删图会与重放不一致，
        因此这里只标记，由后续快照阶段统一裁决。
        """
        if self._graph is None or self._relational is None:
            return []
        weak: list[str] = []
        for record in self._relational.query("synapse", limit=limit):
            subject = str(record.get("subject_id", ""))
            if not subject or subject in weak:
                continue
            try:
                if not self._graph.neighbors(subject, hops=1, limit=1):
                    weak.append(subject)
            except Exception:  # noqa: BLE001  单个实体查询失败不阻断修剪
                continue
        return weak

    def distill_documents(self, *, limit: int = 200, agent_id: str = "distiller",
                          namespace: str = "document") -> dict[str, Any]:
        """兜底：扫描未归档项目档案并提炼（实时线程漏掉或上线前遗留的）。"""
        if self._save_memory is None:
            return {"ok": False, "error": "未注入 save_memory，跳过档案蒸馏"}
        return _distill_documents(
            self._save_memory, limit=limit, agent_id=agent_id, namespace=namespace
        )

    def nightly(self, *, consolidations: int = 0, agent_id: str | None = None) -> NightlyReport:
        report = NightlyReport(emotional=self.emotional_review(agent_id=agent_id))
        report.archived = self.forget_sweep()
        report.tier_changes = len(report.archived)
        report.weak_relations = self.graph_prune()
        distill = self.distill_documents(agent_id=agent_id or "distiller")
        report.distilled_docs = int(distill.get("distilled", 0))
        report.neurons_written = int(distill.get("written", 0))
        report.consolidations = consolidations
        return report

    def morning_brief(self, *, agent_id: str | None = None) -> str:
        return self.nightly(agent_id=agent_id).as_brief()
