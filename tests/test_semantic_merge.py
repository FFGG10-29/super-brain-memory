"""LLM 语义合并裁决 + 聚簇主题摘要 验收。"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from brain.memory_consolidator import MemoryConsolidator
from config import BrainConfig, ModelConfig, StorageConfig, SyncConfig
from main import build_app


class ScriptedLLM:
    """脚本化 LLM：按顺序返回裁决 JSON；fail=True 时抛异常（模拟不可用）。"""

    degraded = False

    def __init__(self, responses: list[dict] | None = None, *, fail: bool = False) -> None:
        self.responses = list(responses or [])
        self.fail = fail
        self.prompts: list[str] = []

    def complete_json(self, system_prompt, user_prompt, *, required_keys=(), max_retries=2) -> Mapping[str, Any]:
        self.prompts.append(user_prompt)
        if self.fail:
            raise Exception("LLM down")
        return self.responses.pop(0) if self.responses else {"action": "add", "reason": "default"}


@pytest.fixture()
def desktop_config(tmp_path):
    return BrainConfig(
        storage=StorageConfig(
            sqlite_path=tmp_path / "r.db", lancedb_path=tmp_path / "v.lance",
            kuzu_path=tmp_path / "g.kuzu", lmdb_path=tmp_path / "kv.lmdb", duckdb_path=tmp_path / "t.db",
        ),
        model=ModelConfig(dense_dim=8, use_cuda=False, llm_gguf_dir=tmp_path / "models"),
        sync=SyncConfig(device_id="dev-merge", local_repo_dir=tmp_path / "sync"),
    )


def _build_with_scripted_llm(desktop_config, llm):
    app = build_app(desktop_config)
    # 用脚本化 LLM 替换内核里的 LLM（consolidator 持有同一引用）
    app.llm = llm  # type: ignore[assignment]
    consolidator = MemoryConsolidator(
        llm=llm, relational=app.storage.relational, vector=app.storage.vector,
        embedder=app.embedder, config=desktop_config,
    )
    app.hippocampus._consolidator = consolidator
    return app, consolidator


def test_merge_update_supersedes_old_memory(desktop_config):
    app = build_app(desktop_config)
    old = app.hippocampus.remember("项目排期定在 5 月 20 日", agent_id="tester")
    app.close()

    llm = ScriptedLLM(responses=[
        {"action": "update", "content": "项目排期从 5 月 20 日推迟到 6 月 1 日", "supersede_ids": [old.neuron.uuid7_id], "reason": "排期变更"},
    ])
    app2, _ = _build_with_scripted_llm(desktop_config, llm)
    try:
        print("\nMERGE_ENABLED:", app2.config.merge.merge_enabled, flush=True)
        result = app2.hippocampus.remember("项目排期定了，不过推迟了", agent_id="tester")
        print("DECISION:", result.merge, "APPLIED:", result.applied,
              "REASON:", result.admission.reason, flush=True)
        assert result.applied is True
        assert result.merge is not None and result.merge.action == "update"
        assert old.neuron.uuid7_id in result.superseded
        old_row = app2.storage.relational.get("neuron", old.neuron.uuid7_id)
        assert old_row is not None and not old_row.get("is_active", True), "被合并的旧记忆必须封存而非删除"
        assert "6 月 1 日" in str(app2.storage.relational.get("neuron", result.neuron.uuid7_id)["content"])
    finally:
        app2.close()


def test_llm_failure_falls_back_to_add(desktop_config):
    llm = ScriptedLLM(fail=True)
    app, consolidator = _build_with_scripted_llm(desktop_config, llm)
    try:
        app.hippocampus.remember("已有记忆：缓存服务迁移完成", agent_id="tester")
        decision = consolidator.decide("缓存服务迁移完成，并且回滚预案已更新", agent_id="tester")
        assert decision.action == "add"
        assert "LLM 不可用" in decision.reason, "有候选时 LLM 失败必须显式兜底为新增"
        result = app.hippocampus.remember("缓存服务迁移完成，并且回滚预案已更新", agent_id="tester")
        assert result.applied is True
    finally:
        app.close()


def test_skip_prevents_redundant_write(desktop_config):
    app = build_app(desktop_config)
    existing = app.hippocampus.remember("用户喜欢简洁的汇报方式", agent_id="tester")
    app.close()

    llm = ScriptedLLM(responses=[{"action": "skip", "reason": "信息已包含在旧记忆中"}])
    app2, _ = _build_with_scripted_llm(desktop_config, llm)
    try:
        result = app2.hippocampus.remember("用户喜欢简洁的汇报方式（更啰嗦的表述）", agent_id="tester")
        assert result.applied is False
        assert result.merge is not None and result.merge.action == "skip"
        row = app2.storage.relational.get("neuron", existing.neuron.uuid7_id)
        assert row is not None and row.get("is_active", True), "skip 不应封存旧记忆"
    finally:
        app2.close()


def test_merge_disabled_skips_llm(desktop_config):
    desktop_config.merge.merge_enabled = False
    llm = ScriptedLLM(responses=[{"action": "merge", "content": "x", "supersede_ids": ["fake"]}])
    app, consolidator = _build_with_scripted_llm(desktop_config, llm)
    try:
        decision = consolidator.decide("任意内容", agent_id="tester")
        assert decision.action == "add"
        assert llm.prompts == [], "关闭合并时不得调用 LLM"
    finally:
        app.close()


def test_cluster_summary_written_via_full_pipeline(desktop_config):
    llm = ScriptedLLM(responses=[
        {"action": "add", "reason": "写入候选 1"},
        {"action": "add", "reason": "写入候选 2"},
        {"summary": "用户在迁移缓存服务：新机房、限流策略与回滚预案均已确定"},
    ])
    app, consolidator = _build_with_scripted_llm(desktop_config, llm)
    try:
        for text in (
            "缓存服务迁移到新机房完成了",
            "缓存服务新机房的限流策略已配置",
            "缓存服务迁移的回滚预案已演练",
            "一条无关记忆：今天天气不错",
        ):
            w = app.hippocampus.remember(text, agent_id="tester")
            print("\nWRITE:", text[:12], "applied:", w.applied,
                  "accepted:", w.admission.accepted, "reason:", w.admission.reason,
                  "merge:", w.merge and w.merge.action, flush=True)
        consolidations = app.neocortex.consolidate()
        def _remember(text: str, agent_id: str = "consolidator"):
            w = app.hippocampus.remember(text, agent_id=agent_id)
            print("INNER WRITE:", w.applied, "accepted:", w.admission.accepted,
                  "reason:", w.admission.reason, "| merge:",
                  (w.merge.action, w.merge.reason) if w.merge else None, flush=True)
            return w

        summaries = consolidator.summarize_clusters(consolidations, remember=_remember)
        for s in summaries:
            print("SUMMARY:", s.applied, s.label, s.summary[:30], flush=True)
        assert summaries, "有足够成员的簇必须产出摘要"
        assert summaries[0].applied is True
        contents = [row["content"] for row in app.storage.relational.query("neuron", limit=100)]
        assert any("【主题摘要】" in item and "缓存服务" in item for item in contents)
    finally:
        app.close()
