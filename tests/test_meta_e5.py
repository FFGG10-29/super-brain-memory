"""E5 / Q32 验收：再巩固窗口（reconsolidation window）。

裁决：窗口内合并目标保留（id 不变、无新版本行），但**必须产生 reconsolidate
事件**（不静默改 payload）；窗口外 / 覆盖式路径 / 非认知性目标的行为与旧版完全一致。
"""
from __future__ import annotations

from brain.memory_consolidator import MergeDecision

SCENARIO_COG = {"memory_type": "scenario", "confidence": 0.9}
CODE_COG = {"memory_type": "code", "confidence": 0.9}


class StubConsolidator:
    """脚本化合并裁决：固定返回一个 MergeDecision。"""

    def __init__(self, decision: MergeDecision) -> None:
        self.decision = decision
        self.calls: list[str] = []

    def decide(self, content: str, *, agent_id: str, namespace: str = "private") -> MergeDecision:
        self.calls.append(content)
        return self.decision


def _install(hippocampus, decision: MergeDecision) -> StubConsolidator:
    stub = StubConsolidator(decision)
    hippocampus._consolidator = stub
    return stub


def _reconsolidate_events(journal) -> list[dict]:
    return [
        event for event in journal.iter_events(include_self=True)
        if str(event.get("type")) == "reconsolidate"
    ]


def test_in_window_update_reconsolidates(brain_factory):
    """窗口内更新：同 id、payload 带标记、无新版本行、产生 reconsolidate 事件。"""
    bundle = brain_factory(with_journal=True)
    hippo = bundle.hippocampus
    first = hippo.remember("周五团队体检，地点在城东体检中心", agent_id="tester", cognition=dict(SCENARIO_COG))
    assert first.applied
    old_id = first.neuron.uuid7_id
    assert hippo.reinforce(old_id) is not None, "召回强化必须把 last_recall_ms 打进可塑窗口"

    _install(hippo, MergeDecision(
        action="update", content="周五团队体检，地点改到城西体检中心",
        supersede_ids=(old_id,), reason="同一事件的更新",
    ))
    second = hippo.remember("体检地点改到城西体检中心了", agent_id="tester")

    assert second.applied and second.reconsolidated
    assert second.neuron.uuid7_id == old_id, "再巩固必须保持原 id"
    row = bundle.storage.relational.get("neuron", old_id)
    assert row is not None and row.get("is_active") is True, "目标记忆必须保留且 active"
    assert "城西体检中心" in str(row.get("content")), "payload 必须合并进新内容"
    assert row.get("reconsolidation") is True
    assert int(row.get("reconsolidated_at_ms") or 0) > 0
    assert bundle.storage.relational.count("neuron", active_only=False) == 1, "不得产生新版本行"

    events = _reconsolidate_events(bundle.journal)
    assert events, "Q32：窗口内合并必须产生 reconsolidate 事件"
    payload = dict(events[-1].get("payload") or {})
    assert "城东" in str(payload.get("old_content", ""))
    assert "城西" in str(payload.get("new_content", ""))
    assert str((payload.get("neuron") or {}).get("id")) == old_id


def test_out_of_window_keeps_supersede_semantics(brain_factory):
    """last_recall_ms 拨出窗口（2 小时前 > 默认 30min）：行为与旧版一致（ingest+supersede）。"""
    bundle = brain_factory(with_journal=True)
    hippo = bundle.hippocampus
    first = hippo.remember("周五团队体检，地点在城东体检中心", agent_id="tester", cognition=dict(SCENARIO_COG))
    old_id = first.neuron.uuid7_id
    hippo.reinforce(old_id)
    record = dict(bundle.storage.relational.get("neuron", old_id))
    record["last_recall_ms"] = int(record["last_recall_ms"]) - 2 * 60 * 60 * 1000
    bundle.storage.relational.upsert("neuron", record)

    _install(hippo, MergeDecision(
        action="update", content="周五团队体检，地点改到城西体检中心",
        supersede_ids=(old_id,), reason="同一事件的更新",
    ))
    second = hippo.remember("体检地点改到城西体检中心了", agent_id="tester")

    assert second.applied and not second.reconsolidated
    assert second.neuron.uuid7_id != old_id
    assert old_id in second.superseded
    old_row = bundle.storage.relational.get("neuron", old_id)
    assert old_row is not None and not old_row.get("is_active", True), "窗口外必须照旧封存"
    assert "reconsolidation" not in old_row
    assert not _reconsolidate_events(bundle.journal)


def test_zero_window_disables_reconsolidation(brain_factory):
    """window=0：开关关闭，任何 last_recall_ms 都不走再巩固。"""
    bundle = brain_factory(with_journal=True)
    hippo = bundle.hippocampus
    hippo._reconsolidation_window_sec = 0
    first = hippo.remember("周五团队体检，地点在城东体检中心", agent_id="tester", cognition=dict(SCENARIO_COG))
    old_id = first.neuron.uuid7_id
    hippo.reinforce(old_id)

    _install(hippo, MergeDecision(
        action="update", content="周五团队体检，地点改到城西体检中心",
        supersede_ids=(old_id,), reason="同一事件的更新",
    ))
    second = hippo.remember("体检地点改到城西体检中心了", agent_id="tester")

    assert second.applied and not second.reconsolidated
    assert second.neuron.uuid7_id != old_id
    assert old_id in second.superseded
    assert not _reconsolidate_events(bundle.journal)


def test_fact_type_in_window_falls_back(brain_factory):
    """非认知性目标（fact）即使在窗口内也不走再巩固，照旧 supersede。"""
    bundle = brain_factory(with_journal=True)
    hippo = bundle.hippocampus
    fact = hippo.remember("项目排期定在 5 月 20 日", agent_id="tester")
    fact_id = fact.neuron.uuid7_id
    hippo.reinforce(fact_id)

    _install(hippo, MergeDecision(
        action="update", content="项目排期推迟到 6 月 1 日",
        supersede_ids=(fact_id,), reason="排期变更",
    ))
    result = hippo.remember("排期有变化", agent_id="tester")

    assert result.applied and not result.reconsolidated
    assert fact_id in result.superseded
    assert not _reconsolidate_events(bundle.journal)


def test_overwrite_path_unaffected(brain_factory):
    """覆盖式路径（code）不经过合并裁决，再巩固分支不得介入。"""
    bundle = brain_factory(with_journal=True)
    hippo = bundle.hippocampus
    first_code = hippo.remember("def deploy():\n    return 'ok'\n", agent_id="tester", cognition=dict(CODE_COG))
    assert first_code.applied
    second_code = hippo.remember("def deploy():\n    return 'v2'\n", agent_id="tester", cognition=dict(CODE_COG))
    assert second_code.applied
    assert second_code.merge is None, "覆盖式路径不得经过语义合并裁决"
    assert second_code.reconsolidated is False
    assert second_code.neuron.uuid7_id != first_code.neuron.uuid7_id
