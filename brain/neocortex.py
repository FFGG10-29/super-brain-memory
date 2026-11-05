"""brain.neocortex —— 新皮层：金字塔固化（原始 → 事实 → 主题 → 世界观）。

夜间运行：把同 (namespace, memory_type) 的一组原始记忆聚合为主题节点，
产出 consolidate 事件；成员集合必须可追溯，原始记忆不改写。
世界观层（worldview）默认需要人工确认，代码只产出候选，不自动升维。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from config import BrainConfig, get_config
from core.ids import new_uuid7
from core.interfaces import EventLog, RelationalRepository
from models.neuron import Neuron

__all__ = ["Consolidation", "Neocortex"]


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class Consolidation:
    id: str
    label: str
    level: str
    members: tuple[str, ...]
    namespace: str
    memory_type: str


class Neocortex:
    def __init__(
        self,
        relational: RelationalRepository,
        *,
        event_log: EventLog | None = None,
        config: BrainConfig | None = None,
    ) -> None:
        self._relational = relational
        self._event_log = event_log
        self._config = config or get_config()

    def consolidate(self, *, namespace: str | None = None, limit: int = 200, min_members: int = 2) -> list[Consolidation]:
        groups: dict[tuple[str, str], list[Neuron]] = {}
        for record in self._relational.query("neuron", limit=limit):
            neuron = Neuron.from_record(record)
            if namespace and neuron.namespace != namespace:
                continue
            if neuron.pyramid != "raw":
                continue
            groups.setdefault((neuron.namespace, neuron.memory_type), []).append(neuron)

        results: list[Consolidation] = []
        for (ns, memory_type), members in sorted(groups.items()):
            if len(members) < min_members:
                continue
            members.sort(key=lambda item: item.uuid7_id)
            topic_id = new_uuid7()
            label = f"主题：{memory_type}/{ns}（{len(members)} 条）"
            member_ids = tuple(item.uuid7_id for item in members)
            consolidation = Consolidation(topic_id, label, "topic", member_ids, ns, memory_type)
            # 主题节点落主库（hyperedge 表可承载 members），原始记录不动
            self._relational.upsert(
                "hyperedge",
                {
                    "id": topic_id,
                    "label": label,
                    "members": list(member_ids),
                    "namespace": ns,
                    "pyramid": "topic",
                    "is_active": True,
                    "valid_from_ms": _now_ms(),
                    "tx_from": f"{_now_ms()}:0:{self._config.sync.device_id}",
                    "device_id": self._config.sync.device_id,
                    "agent_id": "system",
                },
            )
            self._emit(consolidation)
            results.append(consolidation)
        return results

    def _emit(self, consolidation: Consolidation) -> None:
        if self._event_log is None:
            return
        self._event_log.append(
            {
                "event_id": consolidation.id,
                "type": "consolidate",
                "agent_id": "system",
                "hlc": f"{_now_ms()}:0:{self._config.sync.device_id}",
                "payload": {
                    "topic_id": consolidation.id,
                    "label": consolidation.label,
                    "level": consolidation.level,
                    "members": list(consolidation.members),
                    "namespace": consolidation.namespace,
                },
            }
        )
