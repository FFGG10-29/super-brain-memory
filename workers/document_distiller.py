"""workers.document_distiller —— 项目档案 → 六引擎神经元的提炼桥接。

两条路径共用同一套提炼逻辑：

  1) 实时（主路径）：agent 调 documents_mutate(create/update) 时，后端经 context_layers
     注入的触发器把 doc_id 投递到后台队列，独立线程「有就开始、没有就等」地提炼，
     写入神经元后把源档案归档（不阻塞 agent 的写入返回）。
  2) 夜间/兜底（次要）：life_loop.nightly() 调 distill_documents() 扫一遍未归档档案，
     清掉功能上线前遗留的、或实时提炼失败重试的文档。

提炼后内容进入六引擎 neuron 双轴模型：
    - 金字塔轴 raw → fact → topic → worldview：新神经元默认 pyramid=raw，
      由夜间 neocortex.consolidate() 聚类升维为 topic；
    - 分层轴 buffer → core/recall → archive：由 tier_manager.sweep() 按可提取性推进。

纪律：
    - 只读取 core.context_layers（项目档案）这一独立存储；
    - 写入一律走注入的 save_memory_fn（即 hippocampus.remember），绝不直连任何存储库；
    - 不物理删除任何档案（归档 = archived=True）。
"""
from __future__ import annotations

import hashlib
import queue
import re
import threading
from typing import Any, Callable

from core.cache_manager import get_cache_manager
from core.context_layers import get_context_layers

_MAX_CHUNKS_PER_DOC = 20
_CACHE_NS = "doc_distill"  # C02：蒸馏结果缓存命名空间（lmdb 介质，ttl_sec=0 永久）
# 本进程内 doc_id → 最近一次蒸馏时内容 hash 的记录（enqueue 精确失效的 O(1) 快路径）
_DOC_HASH_BY_ID: dict[str, str] = {}


def _chunk_markdown(text: str) -> list[str]:
    """按一级/二级标题切分；无标题则整篇作为一块（便于新皮层聚类升维）。"""
    text = text or ""
    headings = [m.start() for m in re.finditer(r"(?m)^#{1,2}\s", text)]
    if not headings:
        return [text.strip()] if text.strip() else []
    chunks: list[str] = []
    for i, start in enumerate(headings):
        end = headings[i + 1] if i + 1 < len(headings) else len(text)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
    return chunks[:_MAX_CHUNKS_PER_DOC]


def _content_hash(content: str) -> str:
    """缓存键：文档全文的 sha256 十六进制串（统一约定，键=str(sha256(文档内容))）。"""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _extract_neuron_ids(result: Any) -> list[str]:
    """尽力从 save_memory_fn 的返回值提取产出 neuron id（语义同 cache_invalidation，宁缺勿错）。"""
    ids: list[str] = []
    neuron = getattr(result, "neuron", None)
    if neuron is not None:
        nid = getattr(neuron, "uuid7_id", None) or getattr(neuron, "id", None)
        if nid:
            ids.append(str(nid))
    for extra in (getattr(result, "superseded", None) or []):
        if extra:
            ids.append(str(extra))
    if isinstance(result, dict):
        for key in ("id", "memory_id", "neuron_id"):
            if result.get(key):
                ids.append(str(result[key]))
        for item in (result.get("event_ids") or []):
            if item:
                ids.append(str(item))
        inner = result.get("result")
        if isinstance(inner, dict):
            ids.extend(_extract_neuron_ids(inner))
    return list(dict.fromkeys(item for item in ids if item))


def distill_one(doc_id: str, save_memory_fn: Callable[..., Any], *,
                agent_id: str = "distiller", namespace: str = "document") -> dict[str, Any]:
    """提炼单篇档案：查缓存 → 读全文 → 切块 → 逐块写入神经元 → 归档源档。"""
    layers = get_context_layers()
    full = layers.documents_read(doc_id)
    if not full.get("ok"):
        return {"ok": False, "error": full.get("error")}
    content = full.get("content", "")
    cache_key = _content_hash(content)
    mgr = get_cache_manager()
    # C02：同内容文档二次蒸馏直接命中缓存，跳过 LLM 提炼（written=0, skipped=0）
    if mgr is not None and mgr.get(_CACHE_NS, cache_key) is not None:
        _DOC_HASH_BY_ID[doc_id] = cache_key  # 记录映射，供变更时精确失效
        layers.documents_mutate(action="archive", doc_id=doc_id)
        return {"ok": True, "written": 0, "skipped": 0, "cache_hits": 1, "archived": True}
    chunks = _chunk_markdown(content)
    if not chunks:
        # 空文档也直接归档，避免反复扫描；同时缓存 no-neurons 标记（内容 hash 键稳定）
        if mgr is not None:
            mgr.set(_CACHE_NS, cache_key,
                    {"doc_id": doc_id, "neuron_ids": [], "no_neurons": True}, ttl_sec=0)
            _DOC_HASH_BY_ID[doc_id] = cache_key
        layers.documents_mutate(action="archive", doc_id=doc_id)
        return {"ok": True, "written": 0, "skipped": 0, "cache_hits": 0,
                "archived": True, "neuron_ids": []}
    written = skipped = 0
    neuron_ids: list[str] = []
    for chunk in chunks:
        result = save_memory_fn(chunk, agent_id=agent_id, namespace=namespace)
        if isinstance(result, dict):
            applied = bool(result.get("applied"))
        else:
            applied = bool(getattr(result, "applied", False))
        if applied:
            written += 1
            neuron_ids.extend(_extract_neuron_ids(result))
        else:
            # 被语义合并裁决 skip（已存在等价记忆）也算已提炼，归档即可
            skipped += 1
    if mgr is not None:
        # 值里带 doc_id + 产出 neuron id：前者供 documents_mutate 变更路径按 id 精确失效，
        # 后者供 memory_updated 失效总线（DEFAULT_AFFECTED 已含 doc_distill）精确命中本条。
        # ttl_sec=0 = 永不过期（ns 默认配置即如此，显式传 0 防配置漂移），靠精确失效清场。
        mgr.set(_CACHE_NS, cache_key,
                {"doc_id": doc_id, "neuron_ids": neuron_ids,
                 "no_neurons": not neuron_ids},
                ttl_sec=0)
        _DOC_HASH_BY_ID[doc_id] = cache_key
    # 无论写入还是 skip，都视为已处理：归档源档案（不物理删除）
    layers.documents_mutate(action="archive", doc_id=doc_id)
    return {"ok": True, "written": written, "skipped": skipped, "cache_hits": 0,
            "archived": True, "neuron_ids": neuron_ids}


# ── 实时后台线程（事件驱动：来一条处理一条，空闲则阻塞等待）──────────────────
class _DistillWorker:
    def __init__(self) -> None:
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._save: Callable[..., Any] | None = None
        self._thread: threading.Thread | None = None

    def start(self, save_memory_fn: Callable[..., Any]) -> None:
        self._save = save_memory_fn
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="doc-distill", daemon=True)
        self._thread.start()

    def enqueue(self, doc_id: str) -> None:
        if self._save is None:
            return
        self._queue.put(doc_id)

    def _run(self) -> None:
        save = self._save
        assert save is not None
        while True:
            doc_id = self._queue.get()  # 阻塞：没有就等，有就开始
            try:
                distill_one(doc_id, save)
            except Exception:  # noqa: BLE001  单档失败已记录在档内，不阻塞线程
                pass
            self._queue.task_done()


_WORKER = _DistillWorker()


def start_distill_worker(save_memory_fn: Callable[..., Any]) -> None:
    _WORKER.start(save_memory_fn)


def _invalidate_doc_cache(doc_id: str) -> int:
    """documents_mutate(create/update) 变更路径：按文档 id 精确失效 doc_distill 缓存。

    键是「内容 sha256」，本钩子只拿得到 doc_id，故两条路：
    1) 本进程蒸馏过该文档 → _DOC_HASH_BY_ID 有旧内容 hash，按键 O(1) 删除；
    2) 兜底借用 CacheManager.invalidate_by_event 的「键或值命中 affected_id」精确匹配——
       缓存值里始终写有 doc_id，构造的受控事件 affected_namespaces 只含 doc_distill，
       绝不波及其他命名空间、绝不清空命名空间（C0.2 纪律）。
    局限说明：若某条缓存由外部直写且值中不含该 doc_id（本实现必然写入 doc_id，仅理论
    存在），值匹配找不到它；该条目键是旧内容 hash，内容已变后天然永不命中，
    只占用容量，不影响正确性。
    """
    mgr = get_cache_manager()
    if mgr is None or not doc_id:
        return 0
    removed = 0
    old_hash = _DOC_HASH_BY_ID.pop(doc_id, None)
    if old_hash:
        mgr.delete(_CACHE_NS, old_hash)
        removed += 1
    removed += mgr.invalidate_by_event({
        "type": "memory_updated",
        "event_type": "documents_mutate",
        "affected_ids": [doc_id],
        "affected_namespaces": [_CACHE_NS],
    })
    return removed


def enqueue_document(doc_id: str) -> None:
    # 文档内容已变化：先精确失效旧内容 hash 的缓存条目（防内容回退 A→B→A 时命中过期产出），
    # 再投递后台队列重新提炼
    _invalidate_doc_cache(doc_id)
    _WORKER.enqueue(doc_id)


def distill_documents(
    save_memory_fn: Callable[..., Any],
    *,
    limit: int = 200,
    agent_id: str = "distiller",
    namespace: str = "document",
) -> dict[str, Any]:
    """兜底批处理：扫描未归档档案并逐篇提炼（夜间 life_loop 调用）。"""
    layers = get_context_layers()
    docs = layers.documents_list(include_archived=False)
    if not docs:
        return {"ok": True, "scanned": 0, "distilled": 0, "written": 0,
                "skipped": 0, "cache_hits": 0, "archived": 0, "errors": []}
    docs = docs[: max(1, int(limit))]

    distilled = written = skipped = cache_hits = archived = 0
    errors: list[str] = []
    for doc in docs:
        doc_id = doc.get("id")
        if not doc_id:
            continue
        try:
            res = distill_one(doc_id, save_memory_fn, agent_id=agent_id, namespace=namespace)
            if not res.get("ok"):
                errors.append(f"{doc_id}: {res.get('error')}")
                continue
            distilled += 1
            archived += 1 if res.get("archived") else 0
            written += int(res.get("written", 0))
            skipped += int(res.get("skipped", 0))
            cache_hits += int(res.get("cache_hits", 0))
        except Exception as exc:  # noqa: BLE001  单档失败不阻断其余
            errors.append(f"{doc_id}: {type(exc).__name__}: {exc}")
    return {"ok": True, "scanned": len(docs), "distilled": distilled,
            "written": written, "skipped": skipped, "cache_hits": cache_hits,
            "archived": archived,
            "errors": errors}
