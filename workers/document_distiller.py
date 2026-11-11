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

import queue
import re
import threading
from typing import Any, Callable

from core.context_layers import get_context_layers

_MAX_CHUNKS_PER_DOC = 20


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


def distill_one(doc_id: str, save_memory_fn: Callable[..., Any], *,
                agent_id: str = "distiller", namespace: str = "document") -> dict[str, Any]:
    """提炼单篇档案：读全文 → 切块 → 逐块写入神经元 → 归档源档。"""
    layers = get_context_layers()
    full = layers.documents_read(doc_id)
    if not full.get("ok"):
        return {"ok": False, "error": full.get("error")}
    content = full.get("content", "")
    chunks = _chunk_markdown(content)
    if not chunks:
        # 空文档也直接归档，避免反复扫描
        layers.documents_mutate(action="archive", doc_id=doc_id)
        return {"ok": True, "written": 0, "skipped": 0, "archived": True}
    written = skipped = 0
    for chunk in chunks:
        result = save_memory_fn(chunk, agent_id=agent_id, namespace=namespace)
        if isinstance(result, dict):
            applied = bool(result.get("applied"))
        else:
            applied = bool(getattr(result, "applied", False))
        if applied:
            written += 1
        else:
            # 被语义合并裁决 skip（已存在等价记忆）也算已提炼，归档即可
            skipped += 1
    # 无论写入还是 skip，都视为已处理：归档源档案（不物理删除）
    layers.documents_mutate(action="archive", doc_id=doc_id)
    return {"ok": True, "written": written, "skipped": skipped, "archived": True}


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


def enqueue_document(doc_id: str) -> None:
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
                "skipped": 0, "archived": 0, "errors": []}
    docs = docs[: max(1, int(limit))]

    distilled = written = skipped = archived = 0
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
        except Exception as exc:  # noqa: BLE001  单档失败不阻断其余
            errors.append(f"{doc_id}: {type(exc).__name__}: {exc}")
    return {"ok": True, "scanned": len(docs), "distilled": distilled,
            "written": written, "skipped": skipped, "archived": archived,
            "errors": errors}
