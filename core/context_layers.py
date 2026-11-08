"""core.context_layers —— 分层上下文（对齐 dsh-mnemon 的 View-based 三层模型）。

dsh-mnemon 把上下文拆成三种 Source 角色，由 Strategy 组合成一个 View：

    working-context   运行时记忆   MEMORY.md / USER.md，每轮确定性注入，带字节上限
    narrative         项目档案     先检索摘要，按需读全文，接近容量上限时归档最久未用
    durable-evidence  记忆空间     多空间 + Provider，激活的空间才参与召回

本模块用纯标准库实现同一套语义，落盘在六引擎实际数据根的 context/（与
relational.db 同目录，本地、可备份、可人工编辑），不引入任何第三方依赖，
也不碰六引擎存储。技能层沿用同样的「目录 + 索引 + 版本」结构，带使用
次数 / 成功率 / 自进化。

对外是进程级单例 get_context_layers()，供 v3 RPC 与 MCP 注入共用。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from core.runtime_paths import data_dir

__all__ = ["ContextLayers", "get_context_layers"]

_IMPORTANCE_ORDER = {"low": 0, "normal": 1, "critical": 2}
_DEFAULT_MEMORY_LIMIT = 8000
_DEFAULT_USER_LIMIT = 4000
_DOC_SOFT_LIMIT = 200          # 超过该数量时归档最久未读的文档


def _now_ms() -> int:
    return int(time.time() * 1000)


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _tokens(text: str) -> set[str]:
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    han = re.findall(r"[\u4e00-\u9fff]", lowered)
    bigrams = {han[i] + han[i + 1] for i in range(len(han) - 1)}
    return words | bigrams | set(han)


def _score(query: str, *fields: str) -> float:
    q = _tokens(query)
    if not q:
        return 0.0
    hits = sum(1 for token in q if any(token in (f or "").lower() for f in fields))
    return round(hits / len(q), 4)


def _default_root() -> Path:
    """分层上下文与六引擎记忆落在同一个数据根。

    以 config.storage.sqlite_path 的父目录为准（后端真正打开的库所在目录），
    避免运行时记忆 / 档案 / 技能散落在 %APPDATA%，而 1430 条主记忆却在 D 盘：
    两套目录会让备份、迁移、卸载各搬各的，甚至出现小库覆盖真库的风险。
    """
    try:
        from config import get_config

        return Path(get_config().storage.sqlite_path).expanduser().resolve().parent / "context"
    except Exception:  # noqa: BLE001 - 配置未就绪时退回 data_dir()
        return data_dir() / "context"


class ContextLayers:
    """三层上下文 + 技能层。所有写操作加锁，索引原子替换。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else _default_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._docs = self.root / "documents"
        self._skills = self.root / "skills"
        self._archive = self.root / "archive"
        for folder in (self._docs, self._skills, self._archive):
            folder.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._ensure_defaults()

    # ── 初始化 ────────────────────────────────────────────────────────────
    @property
    def _runtime_path(self) -> Path:
        return self.root / "runtime.json"

    @property
    def _doc_index_path(self) -> Path:
        return self._docs / "index.json"

    @property
    def _spaces_path(self) -> Path:
        return self.root / "spaces.json"

    @property
    def _skill_index_path(self) -> Path:
        return self._skills / "index.json"

    def _ensure_defaults(self) -> None:
        with self._lock:
            if not self._runtime_path.exists():
                _atomic_json(self._runtime_path, {
                    "revision": 1,
                    "limits": {"memory": _DEFAULT_MEMORY_LIMIT, "user": _DEFAULT_USER_LIMIT},
                    "memory": [], "user": [], "archived": [],
                })
            if not self._doc_index_path.exists():
                _atomic_json(self._doc_index_path, {"revision": 1, "documents": []})
            if not self._spaces_path.exists():
                _atomic_json(self._spaces_path, {"revision": 1, "spaces": [
                    {"id": "default", "name": "默认空间", "provider": "super-brain-native",
                     "active": True, "created_ms": _now_ms(), "note": "六引擎主库"},
                ]})
            if not self._skill_index_path.exists():
                _atomic_json(self._skill_index_path, {"revision": 1, "skills": []})

    # ── 状态 ──────────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        with self._lock:
            runtime = _read_json(self._runtime_path, {})
            docs = _read_json(self._doc_index_path, {}).get("documents", [])
            spaces = _read_json(self._spaces_path, {}).get("spaces", [])
            skills = _read_json(self._skill_index_path, {}).get("skills", [])
            limits = runtime.get("limits", {})
            used = {
                "memory": sum(_bytes(str(e.get("content", ""))) for e in runtime.get("memory", [])),
                "user": sum(_bytes(str(e.get("content", ""))) for e in runtime.get("user", [])),
            }
            return {
                "root": str(self.root),
                "revision": runtime.get("revision", 1),
                "layers": {
                    "runtime": {"entries": len(runtime.get("memory", [])) + len(runtime.get("user", [])),
                                "used_bytes": used, "limits": limits,
                                "archived": len(runtime.get("archived", []))},
                    "documents": {"total": len(docs),
                                  "active": sum(1 for d in docs if not d.get("archived")),
                                  "soft_limit": _DOC_SOFT_LIMIT},
                    "spaces": {"total": len(spaces),
                               "active": sum(1 for s in spaces if s.get("active"))},
                    "skills": {"total": len(skills),
                               "avg_success": round(sum(float(s.get("success_rate", 0)) for s in skills) / len(skills), 3) if skills else 0.0},
                },
                "providers": sorted({str(s.get("provider", "")) for s in spaces}),
            }

    # ── 运行时记忆（working-context）────────────────────────────────────
    def runtime_get(self) -> dict[str, Any]:
        with self._lock:
            data = _read_json(self._runtime_path, {})
        return {"ok": True, **data}

    def runtime_projection(self, max_entries: int = 6) -> str:
        """每轮确定性注入的紧凑文本；按 importance 排序，绝不超预算。"""
        with self._lock:
            data = _read_json(self._runtime_path, {})
        entries = [("MEMORY", e) for e in data.get("memory", [])]
        entries += [("USER", e) for e in data.get("user", [])]
        entries.sort(key=lambda pair: _IMPORTANCE_ORDER.get(pair[1].get("importance"), 1), reverse=True)
        lines = ["【运行时记忆】每轮必读，优先级 critical > normal > low"]
        for target, entry in entries[:max_entries]:
            lines.append(f"- [{target}/{entry.get('importance', 'normal')}] {entry.get('content', '')}")
        return "\n".join(lines) if len(entries) > 0 else ""

    def runtime_mutate(self, action: str, target: str, content: str = "",
                       old_text: str = "", importance: str = "normal") -> dict[str, Any]:
        """add / replace / remove。写满时把 low→normal 的最旧条目移入 archived（不删除）。"""
        if target not in ("memory", "user"):
            return {"ok": False, "error": "target 必须是 memory 或 user"}
        if action not in ("add", "replace", "remove"):
            return {"ok": False, "error": "action 必须是 add / replace / remove"}
        if importance not in _IMPORTANCE_ORDER:
            importance = "normal"
        with self._lock:
            data = _read_json(self._runtime_path, {})
            bucket: list[dict[str, Any]] = list(data.get(target, []))
            if action == "add":
                bucket.append({"id": uuid.uuid4().hex[:12], "content": content.strip(),
                               "importance": importance, "updated_ms": _now_ms()})
            else:
                index = next((i for i, e in enumerate(bucket)
                              if old_text and (old_text in str(e.get("content", "")))), -1)
                if index < 0:
                    return {"ok": False, "error": "未找到匹配的条目，请传 old_text"}
                if action == "replace":
                    bucket[index] = {**bucket[index], "content": content.strip(),
                                     "importance": importance, "updated_ms": _now_ms()}
                else:
                    bucket.pop(index)
            limit = int(data.get("limits", {}).get(target, _DEFAULT_MEMORY_LIMIT))
            archived: list[dict[str, Any]] = list(data.get("archived", []))
            while sum(_bytes(str(e.get("content", ""))) for e in bucket) > limit:
                victim = min(range(len(bucket)),
                            key=lambda i: (_IMPORTANCE_ORDER.get(bucket[i].get("importance"), 1),
                                           bucket[i].get("updated_ms", 0)))
                moved = bucket.pop(victim)
                archived.append({**moved, "target": target, "archived_ms": _now_ms()})
            data[target] = bucket
            data["archived"] = archived[-500:]
            data["revision"] = int(data.get("revision", 1)) + 1
            _atomic_json(self._runtime_path, data)
        return {"ok": True, "revision": data["revision"], "entries": len(bucket),
                "archived": len(archived)}

    # ── 项目档案（narrative）─────────────────────────────────────────────
    def documents_list(self, keyword: str = "", include_archived: bool = False) -> list[dict[str, Any]]:
        """只给摘要，不返回全文——由调用方按需 documents_read()。"""
        with self._lock:
            rows = _read_json(self._doc_index_path, {}).get("documents", [])
        out = []
        for row in rows:
            if row.get("archived") and not include_archived:
                continue
            score = _score(keyword, row.get("title", ""), row.get("description", "")) if keyword else 1.0
            if keyword and score <= 0:
                continue
            out.append({**{k: row.get(k) for k in
                          ("id", "title", "description", "bytes", "updated_ms", "last_read_ms", "archived", "source_paths")},
                        "score": score, "has_full_text": True})
        out.sort(key=lambda r: (r["score"], r.get("updated_ms") or 0), reverse=True)
        return out

    def documents_search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        return self.documents_list(keyword=query)[:max(1, int(limit))]

    def documents_read(self, doc_id: str) -> dict[str, Any]:
        with self._lock:
            index = _read_json(self._doc_index_path, {})
            rows = list(index.get("documents", []))
            row = next((r for r in rows if r.get("id") == doc_id), None)
            if row is None:
                return {"ok": False, "error": f"文档不存在：{doc_id}"}
            path = self._docs / f"{doc_id}.md"
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                content = ""
            row["last_read_ms"] = _now_ms()
            row["read_count"] = int(row.get("read_count", 0)) + 1
            index["documents"] = rows
            _atomic_json(self._doc_index_path, index)
        return {"ok": True, "id": doc_id, "title": row.get("title", ""),
                "content": content, "bytes": _bytes(content)}

    def documents_mutate(self, action: str, doc_id: str = "", title: str = "",
                         description: str = "", content: str = "",
                         source_paths: list[str] | None = None) -> dict[str, Any]:
        with self._lock:
            index = _read_json(self._doc_index_path, {})
            rows: list[dict[str, Any]] = list(index.get("documents", []))
            if action == "create":
                doc_id = uuid.uuid4().hex[:12]
                if not title.strip():
                    return {"ok": False, "error": "title 不能为空"}
                rows.append({"id": doc_id, "title": title.strip(), "description": description.strip(),
                             "bytes": _bytes(content), "updated_ms": _now_ms(), "last_read_ms": 0,
                             "read_count": 0, "archived": False, "source_paths": source_paths or []})
                (self._docs / f"{doc_id}.md").write_text(content, encoding="utf-8")
            elif action == "update":
                row = next((r for r in rows if r.get("id") == doc_id), None)
                if row is None:
                    return {"ok": False, "error": f"文档不存在：{doc_id}"}
                if title.strip():
                    row["title"] = title.strip()
                if description.strip():
                    row["description"] = description.strip()
                if content:
                    (self._docs / f"{doc_id}.md").write_text(content, encoding="utf-8")
                    row["bytes"] = _bytes(content)
                row["updated_ms"] = _now_ms()
            elif action == "archive":
                row = next((r for r in rows if r.get("id") == doc_id), None)
                if row is None:
                    return {"ok": False, "error": f"文档不存在：{doc_id}"}
                row["archived"] = True
            elif action == "delete":
                rows = [r for r in rows if r.get("id") != doc_id]
                try:
                    (self._docs / f"{doc_id}.md").unlink()
                except OSError:
                    pass
            else:
                return {"ok": False, "error": "action 必须是 create / update / archive / delete"}
            index["documents"] = rows
            index["revision"] = int(index.get("revision", 1)) + 1
            _atomic_json(self._doc_index_path, index)
        return {"ok": True, "id": doc_id, "total": len(rows)}

    # ── 记忆空间（durable-evidence）─────────────────────────────────────
    def spaces_list(self) -> dict[str, Any]:
        with self._lock:
            data = _read_json(self._spaces_path, {})
        return {"ok": True, **data}

    def spaces_create(self, name: str, provider: str = "super-brain-native", note: str = "") -> dict[str, Any]:
        with self._lock:
            data = _read_json(self._spaces_path, {})
            rows = list(data.get("spaces", []))
            space = {"id": uuid.uuid4().hex[:12], "name": name.strip() or "新空间",
                     "provider": provider, "active": True, "created_ms": _now_ms(), "note": note}
            rows.append(space)
            data["spaces"] = rows
            data["revision"] = int(data.get("revision", 1)) + 1
            _atomic_json(self._spaces_path, data)
        return {"ok": True, "space": space}

    def spaces_activate(self, space_id: str, active: bool = True) -> dict[str, Any]:
        with self._lock:
            data = _read_json(self._spaces_path, {})
            rows = list(data.get("spaces", []))
            row = next((r for r in rows if r.get("id") == space_id), None)
            if row is None:
                return {"ok": False, "error": f"空间不存在：{space_id}"}
            row["active"] = bool(active)
            data["spaces"] = rows
            data["revision"] = int(data.get("revision", 1)) + 1
            _atomic_json(self._spaces_path, data)
        return {"ok": True, "id": space_id, "active": bool(active)}

    # ── 技能（自进化）────────────────────────────────────────────────────
    def skills_list(self) -> dict[str, Any]:
        with self._lock:
            data = _read_json(self._skill_index_path, {})
        rows = data.get("skills", [])
        return {"ok": True, "skills": [{k: s.get(k) for k in
                ("id", "name", "description", "version", "usage_count", "success_rate", "tags", "updated_ms")}
                for s in rows]}

    def skills_get(self, skill_id: str) -> dict[str, Any]:
        with self._lock:
            data = _read_json(self._skill_index_path, {})
            row = next((s for s in data.get("skills", []) if s.get("id") == skill_id), None)
        if row is None:
            return {"ok": False, "error": f"技能不存在：{skill_id}"}
        try:
            content = (self._skills / f"{skill_id}.md").read_text(encoding="utf-8")
        except OSError:
            content = ""
        return {"ok": True, **row, "content": content}

    def skills_upsert(self, skill_id: str = "", name: str = "", description: str = "",
                      content: str = "", tags: list[str] | None = None) -> dict[str, Any]:
        with self._lock:
            data = _read_json(self._skill_index_path, {})
            rows: list[dict[str, Any]] = list(data.get("skills", []))
            row = next((s for s in rows if s.get("id") == skill_id), None) if skill_id else None
            if row is None:
                skill_id = uuid.uuid4().hex[:12]
                row = {"id": skill_id, "version": 1, "usage_count": 0, "success_count": 0,
                       "success_rate": 0.0, "created_ms": _now_ms()}
                rows.append(row)
            elif content.strip():
                row["version"] = int(row.get("version", 1)) + 1
            row.update({"name": name.strip() or row.get("name", "未命名技能"),
                        "description": description.strip() or row.get("description", ""),
                        "tags": tags or row.get("tags", []), "updated_ms": _now_ms()})
            if content.strip():
                (self._skills / f"{skill_id}.md").write_text(content, encoding="utf-8")
            data["skills"] = rows
            data["revision"] = int(data.get("revision", 1)) + 1
            _atomic_json(self._skill_index_path, data)
        return {"ok": True, "id": skill_id, "version": row["version"]}

    def skills_evolve(self, skill_id: str, success: bool = True, note: str = "") -> dict[str, Any]:
        """累计使用与成功率；成功率低于 0.5 且使用 >=5 次时标记待改进。"""
        with self._lock:
            data = _read_json(self._skill_index_path, {})
            rows = list(data.get("skills", []))
            row = next((s for s in rows if s.get("id") == skill_id), None)
            if row is None:
                return {"ok": False, "error": f"技能不存在：{skill_id}"}
            row["usage_count"] = int(row.get("usage_count", 0)) + 1
            row["success_count"] = int(row.get("success_count", 0)) + (1 if success else 0)
            row["success_rate"] = round(row["success_count"] / row["usage_count"], 3)
            row["needs_review"] = row["usage_count"] >= 5 and row["success_rate"] < 0.5
            if note.strip():
                row["last_note"] = note.strip()
            row["updated_ms"] = _now_ms()
            data["skills"] = rows
            data["revision"] = int(data.get("revision", 1)) + 1
            _atomic_json(self._skill_index_path, data)
        return {"ok": True, "id": skill_id, **{k: row[k] for k in
                ("version", "usage_count", "success_rate", "needs_review")}}

    def skills_delete(self, skill_id: str) -> dict[str, Any]:
        """删除技能（索引 + 正文）。不可恢复，UI 需二次确认。"""
        with self._lock:
            data = _read_json(self._skill_index_path, {})
            rows: list[dict[str, Any]] = list(data.get("skills", []))
            if not any(row.get("id") == skill_id for row in rows):
                return {"ok": False, "error": f"技能不存在：{skill_id}"}
            data["skills"] = [row for row in rows if row.get("id") != skill_id]
            data["revision"] = int(data.get("revision", 1)) + 1
            _atomic_json(self._skill_index_path, data)
        try:
            (self._skills / f"{skill_id}.md").unlink()
        except OSError:
            pass
        return {"ok": True, "id": skill_id, "remaining": len(data["skills"])}

    # ── View 组合（Strategy 语义）────────────────────────────────────────
    def compose(self, query: str = "", max_runtime: int = 6, max_docs: int = 3) -> str:
        """把三层拼成一段有预算的注入文本，等价于 dsh-mnemon 的 View projection。"""
        parts: list[str] = []
        runtime = self.runtime_projection(max_entries=max_runtime)
        if runtime:
            parts.append(runtime)
        docs = self.documents_search(query, limit=max_docs) if query.strip() else []
        if docs:
            lines = ["【项目档案摘要】仅标题与描述，需要细节请单独读取全文"]
            for doc in docs:
                lines.append(f"- #{doc['id']} {doc.get('title', '')} — {doc.get('description', '')[:120]}")
            parts.append("\n".join(lines))
        spaces = self.spaces_list().get("spaces", [])
        active = [s for s in spaces if s.get("active")]
        if active:
            parts.append("【激活记忆空间】" + "、".join(
                f"{s.get('name')}({s.get('provider')})" for s in active))
        return "\n\n".join(parts)


_LAYERS: ContextLayers | None = None
_LAYERS_LOCK = threading.Lock()


def get_context_layers() -> ContextLayers:
    global _LAYERS
    if _LAYERS is None:
        with _LAYERS_LOCK:
            if _LAYERS is None:
                _LAYERS = ContextLayers()
    return _LAYERS
