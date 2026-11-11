"""C02 文档蒸馏缓存验收：命中跳过 LLM、变更后重新提炼、值含 neuron id 供总线精确失效。"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import core.cache_manager as cache_manager_mod
import pytest

import workers.document_distiller as dd
from config import BrainConfig
from core.cache_manager import configure_cache_manager
from core.context_layers import ContextLayers

DOC_V1 = "# 标题\n\n第一版正文内容，需要被提炼。\n"
DOC_V2 = "# 标题\n\n第二版完全不同的正文内容。\n"


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class FakeSaver:
    """fake save_memory_fn：统计调用次数，返回可提取 neuron id 的 dict。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, chunk: str, *, agent_id: str = "distiller",
                 namespace: str = "document") -> dict:
        self.calls.append(chunk)
        return {"applied": True, "id": f"neuron-{len(self.calls)}"}


@pytest.fixture()
def env(tmp_path, monkeypatch, storage):
    """隔离的 ContextLayers（重定向 tmp）+ 绑定 storage.kv 的 lmdb 缓存管理器。"""
    layers = ContextLayers(root=tmp_path / "context")
    monkeypatch.setattr(dd, "get_context_layers", lambda: layers)
    # 进程级单例隔离：结束还原，避免污染其他用例
    monkeypatch.setattr(cache_manager_mod, "_MANAGER", None)
    configure_cache_manager(config=BrainConfig(_env_file=None), kv=storage.kv)
    dd._DOC_HASH_BY_ID.clear()
    yield SimpleNamespace(layers=layers)
    dd._DOC_HASH_BY_ID.clear()


def _create_doc(env, content: str) -> str:
    res = env.layers.documents_mutate(action="create", title="项目档案", content=content)
    assert res.get("ok")
    return res["id"]


def test_second_distill_hits_cache_zero_llm_calls(env) -> None:
    doc_id = _create_doc(env, DOC_V1)
    saver = FakeSaver()

    first = dd.distill_one(doc_id, saver)
    assert first["ok"] and first["written"] == 1
    assert first["cache_hits"] == 0 and first["archived"]
    assert len(saver.calls) == 1

    # 同内容二次蒸馏：命中缓存，零 LLM 调用，仍视为已处理并归档
    second = dd.distill_one(doc_id, saver)
    assert second["ok"] and second["cache_hits"] == 1
    assert second["written"] == 0 and second["skipped"] == 0
    assert second["archived"] and len(saver.calls) == 1  # LLM 调用数未变


def test_cache_value_contains_doc_id_and_neuron_ids(env) -> None:
    doc_id = _create_doc(env, DOC_V1)
    saver = FakeSaver()
    dd.distill_one(doc_id, saver)

    mgr = dd.get_cache_manager()
    assert mgr is not None
    value = mgr.get(dd._CACHE_NS, _sha(DOC_V1))
    assert value == {"doc_id": doc_id, "neuron_ids": ["neuron-1"], "no_neurons": False}

    # memory_updated 失效总线：产出 neuron id 被更新 → 值命中 → 该条精确失效
    removed = mgr.invalidate_by_event({
        "type": "memory_updated", "affected_ids": ["neuron-1"],
        "affected_namespaces": [dd._CACHE_NS],
    })
    assert removed == 1
    assert mgr.get(dd._CACHE_NS, _sha(DOC_V1)) is None


def test_update_path_invalidates_then_re_distills(env) -> None:
    doc_id = _create_doc(env, DOC_V1)
    saver = FakeSaver()
    dd.distill_one(doc_id, saver)
    assert len(saver.calls) == 1

    mgr = dd.get_cache_manager()
    assert mgr is not None
    assert mgr.get(dd._CACHE_NS, _sha(DOC_V1)) is not None

    # documents_mutate(update) 的实时路径经 set_distill_trigger → enqueue_document：
    # enqueue 前必须把旧内容 hash 的缓存条目精确失效
    res = env.layers.documents_mutate(action="update", doc_id=doc_id, content=DOC_V2)
    assert res.get("ok")
    dd.enqueue_document(doc_id)  # 真实接线即 set_distill_trigger(enqueue_document)
    assert mgr.get(dd._CACHE_NS, _sha(DOC_V1)) is None      # 旧条目已精确失效
    assert mgr.get(dd._CACHE_NS, _sha(DOC_V2)) is None      # 新条目尚未产出

    # 变更后重新提炼：走 LLM（新 hash 未命中），旧条目不复活
    second = dd.distill_one(doc_id, saver)
    assert second["ok"] and second["cache_hits"] == 0 and second["written"] == 1
    assert len(saver.calls) == 2
    assert mgr.get(dd._CACHE_NS, _sha(DOC_V2)) is not None  # 新内容已入缓存


def test_batch_distill_aggregates_cache_hits(env) -> None:
    # 两篇同内容文档：第一篇走 LLM 入缓存，第二篇按内容 hash 命中、零调用
    _create_doc(env, DOC_V1)
    _create_doc(env, DOC_V1)
    saver = FakeSaver()

    res = dd.distill_documents(saver)
    assert res["ok"] and res["distilled"] == 2
    assert res["written"] == 1 and res["skipped"] == 0 and res["cache_hits"] == 1
    assert len(saver.calls) == 1  # 全程只产生 1 次 LLM 调用
