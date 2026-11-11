"""core.persona —— L0 人格层（大肥鱼）单一权威源。

设计来自「三层正交模型」：**空间是主题标签，不是隔离容器**。
    L0 人格层：骨架只读（基线全文存 documents）、可调参数版本化、写入受门槛保护
    L1 事实/项目层：topic 分区记忆，Bot 全量可读
    L2 会话/事件层：反馈事件驱动人格演化

本模块只读写 context/persona.json（与主记忆库同一数据根、可备份、可人工编辑）；
读路径经 core.cache_manager 的进程内快照（persona ns，ttl=0：写路径精确失效 +
外部手改文件按 mtime 自动失效重读）；**不新增表、不改六引擎投影、不碰 Kùzu 单写锁**。
（旧 docstring 声称「镜像一份到 LMDB kv」与实现不符，LMDB 镜像并不存在，已修正。）

护栏（硬边界）：
    1. 骨架字段任何反馈/微调都不可改（需 confirm_skeleton 且留审计）
    2. 反馈驱动的自动微调单次幅度 ≤ 0.05，且必须同向连续 3 次、跨会话 ≥ 2 次
    3. 手动微调不受幅度限制，但每次都落版本，可一键回滚
    4. 全量可读 ≠ 可写人格：MCP 侧由 mcp_server 的 persona_write 门槛把守
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from core.cache_manager import get_cache_manager
from core.context_layers import get_context_layers
from datetime import datetime

__all__ = ["PersonaLayer", "get_persona_layer", "BASELINE_DOC_ID"]

BASELINE_DOC_ID = "cccf1cb8fbad"

# C14/E6 人格参数进程内快照：ns="persona"，键固定 "persona_state"，
# 值 = {"data": persona.json 完整状态 dict, "mtime": 读盘时刻 stat().st_mtime}。
# ttl=0 永不过期；一致性靠 ①写路径精确失效（_write 成功后 invalidate）
# ②命中前 stat 校验 mtime（外部手改文件自动作废重读回填）。
_CACHE_NS = "persona"
_CACHE_KEY = "persona_state"

# 骨架只读字段：身份 / 主人校验 / 底线 / 输出铁律 / 防 OOC 范围 / 记忆调用方式
SKELETON_KEYS = (
    "identity", "master_qq", "boundaries", "output_rules",
    "ooc_scope", "memory_recall_style", "not_lover", "baseline",
)
DEFAULT_PARAMS: dict[str, Any] = {
    "dialect_per_day": 15,          # 方言密度上限（句/天）
    "sassiness": 0.6,               # 斗嘴强度
    "clinginess": 0.6,              # 黏人程度
    "jealousy": 0.5,                # 吃醋阈值
    "care_initiative": 0.5,         # 主动关心触发
    "sleep_nag": True,              # 深夜催睡
    "signature_auto": True,         # 被夸/受委屈/生日自动改签名
    "command_channel": True,        # ★ 闲聊/指令双通道：主人指令不装傻、由 WorkBuddy 执行
    "max_lines_per_reply": 3,       # 每次回复条数上限
    "emotion_baseline": {"p": 0.2, "a": 0.45, "d": 0.5},
}
_NUMERIC_BOUNDS: dict[str, tuple[float, float]] = {
    "dialect_per_day": (0, 30),
    "sassiness": (0, 1),
    "clinginess": (0, 1),
    "jealousy": (0, 1),
    "care_initiative": (0, 1),
    "max_lines_per_reply": (1, 6),
}
_FEEDBACK_MAX_STEP = 0.05           # 自动演化单次幅度上限
_FEEDBACK_STREAK = 3                # 同向连续次数
_FEEDBACK_SESSIONS = 2              # 跨会话数
_HISTORY_MAX = 60
_LOG_MAX = 300


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clamp(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


class PersonaLayer:
    """人格层状态机。进程级单例，所有写操作加锁 + 原子替换。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else get_context_layers().root
        self._file = self.root / "persona.json"
        self._lock = threading.RLock()
        self._ensure()

    # ── 存取 ────────────────────────────────────────────────────────────
    def _ensure(self) -> None:
        with self._lock:
            if not self._file.exists():
                self._write({
                    "revision": 1,
                    "baseline_doc_id": BASELINE_DOC_ID,
                    "baseline_title": "[人格基线] 大肥鱼 v1.1（只读骨架）",
                    "params": json.loads(json.dumps(DEFAULT_PARAMS)),
                    "history": [],
                    "proposals": [],
                    "feedback_log": [],
                    "guardrails": {
                        "skeleton_readonly": True,
                        "feedback_max_step": _FEEDBACK_MAX_STEP,
                        "feedback_streak": _FEEDBACK_STREAK,
                        "feedback_sessions": _FEEDBACK_SESSIONS,
                        "sensitive_never_leaves": True,
                        "execution_belongs_to_workbuddy": True,
                        "bot_reads_all_topics": True,
                    },
                })

    def _read(self) -> dict[str, Any]:
        # C14/E6：先查 persona ns 进程内快照。命中前 stat persona.json 的 mtime，
        # 与快照记录不一致（外部手改 / 同步落盘）→ 快照作废，落到下方读盘回填。
        # stat 失败（文件被原子替换的瞬间 / 已删）时保守沿用快照——磁盘源消失时
        # 缓存是唯一可服务的数据（对齐 C14 用例 test_unchanged_read_serves_from_cache）。
        # 局限：mtime 校验依赖 persona ns 为 memory 介质（默认即是）；若改 LMDB 介质，
        # JSON 往返可能损失 st_mtime 浮点精度，届时需改存整型 ms 时间戳。
        mgr = get_cache_manager()
        if mgr is not None:
            cached = mgr.get(_CACHE_NS, _CACHE_KEY)
            if isinstance(cached, dict) and "data" in cached:
                try:
                    current_mtime: Any = self._file.stat().st_mtime
                except OSError:
                    current_mtime = cached.get("mtime")  # 无法核实 → 沿用快照
                if current_mtime == cached.get("mtime"):
                    return copy.deepcopy(cached["data"])  # 防调用方原地改写污染缓存
            # miss / mtime 漂移 / 值形制不符（旧版裸 dict）→ 统一走读盘回填
        with self._lock:
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
        if mgr is not None and data:
            try:
                mtime: Any = self._file.stat().st_mtime
            except OSError:
                mtime = None
            mgr.set(_CACHE_NS, _CACHE_KEY,
                    {"data": copy.deepcopy(data), "mtime": mtime}, ttl_sec=0)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._file)
        # C14：所有写路径（set_params/rollback/feedback/apply_proposal/set_baseline）
        # 均经此处落盘，成功后对 persona_state 精确失效；mgr 未装配则只更新内存（回退语义）。
        mgr = get_cache_manager()
        if mgr is not None:
            mgr.invalidate(_CACHE_NS, _CACHE_KEY)

    # ── 读 ──────────────────────────────────────────────────────────────
    def get(self) -> dict[str, Any]:
        data = self._read()
        params = dict(DEFAULT_PARAMS)
        params.update(data.get("params") or {})
        layers = get_context_layers()
        baseline = {"id": data.get("baseline_doc_id", BASELINE_DOC_ID),
                    "title": data.get("baseline_title", ""), "exists": False}
        doc = next((d for d in layers.documents_list(keyword="人格基线", include_archived=True)
                    if d.get("id") == baseline["id"]), None)
        if doc:
            baseline["title"] = doc.get("title", baseline["title"])
            baseline["bytes"] = doc.get("bytes", 0)
            baseline["updated_ms"] = doc.get("updated_ms", 0)
            baseline["exists"] = True
        return {
            "ok": True,
            "baseline": baseline,
            "skeleton_keys": list(SKELETON_KEYS),
            "skeleton_readonly": True,
            "params": params,
            "param_bounds": {k: list(v) for k, v in _NUMERIC_BOUNDS.items()},
            "history": (data.get("history") or [])[-12:],
            "history_total": len(data.get("history") or []),
            "proposals": (data.get("proposals") or [])[-20:],
            "feedback_count": len(data.get("feedback_log") or []),
            "guardrails": data.get("guardrails") or {},
            "revision": int(data.get("revision", 1)),
        }

    def baseline_text(self) -> str:
        """骨架全文由项目档案按需读取，不塞进每轮注入。"""
        return get_context_layers().documents_read(self._read().get("baseline_doc_id", BASELINE_DOC_ID)).get("content", "")

    def compose_text(self) -> str:
        """Bot 通道专用注入：骨架只读摘要 + 当前可调参数 + 护栏。"""
        data = self._read()
        p = dict(DEFAULT_PARAMS)
        p.update(data.get("params") or {})
        emo = p.get("emotion_baseline") or {}
        g = data.get("guardrails") or {}
        return "\n".join([
            f"【L0 人格层 · 大肥鱼】骨架只读：档案 #{data.get('baseline_doc_id', BASELINE_DOC_ID)}；"
            "防 OOC 只在闲聊生效；主人下达操作指令走指令通道，由 WorkBuddy 接管执行，不装傻、不挡活。",
            "【可调参数】"
            f"方言≤{p.get('dialect_per_day')}句/天；斗嘴{p.get('sassiness')}；黏人{p.get('clinginess')}；"
            f"吃醋{p.get('jealousy')}；主动关心{p.get('care_initiative')}；催睡={'开' if p.get('sleep_nag') else '关'}；"
            f"签名自动={'开' if p.get('signature_auto') else '关'}；每次≤{p.get('max_lines_per_reply')}条；"
            f"情绪基线 P={emo.get('p')} A={emo.get('a')} D={emo.get('d')}。",
            "【人格护栏】读得到 ≠ 说得出：sensitive 记忆不外发、不进人格；骨架字段任何反馈不可改；"
            "执行权限归 WorkBuddy，不因人格演化放开；"
            f"Bot 全量可读={g.get('bot_reads_all_topics', True)}。",
        ])

    def status(self) -> dict[str, Any]:
        d = self._read()
        return {
            "enabled": True,
            "revision": int(d.get("revision", 1)),
            "baseline_doc_id": d.get("baseline_doc_id", BASELINE_DOC_ID),
            "params_count": len(d.get("params") or {}),
            "versions": len(d.get("history") or []),
            "pending_proposals": sum(1 for x in (d.get("proposals") or []) if x.get("status") == "pending"),
            "feedback_events": len(d.get("feedback_log") or []),
        }

    # ── 手动微调 / 版本 / 回滚 ─────────────────────────────────────────
    def set_params(self, patch: dict[str, Any], *, actor: str = "frontend",
                   reason: str = "", source: str = "manual",
                   confirm_skeleton: bool = False) -> dict[str, Any]:
        if not isinstance(patch, dict) or not patch:
            return {"ok": False, "error": "patch 不能为空"}
        touched_skeleton = [k for k in patch if k in SKELETON_KEYS]
        if touched_skeleton and not confirm_skeleton:
            return {"ok": False, "error": f"骨架只读字段禁止修改：{touched_skeleton}",
                    "skeleton_readonly": True}
        with self._lock:
            data = self._read()
            old = dict(DEFAULT_PARAMS)
            old.update(data.get("params") or {})
            new = json.loads(json.dumps(old))
            changes: list[dict[str, Any]] = []
            for key, value in patch.items():
                if key in SKELETON_KEYS:
                    continue
                if key == "emotion_baseline" and isinstance(value, dict):
                    base = dict(new.get("emotion_baseline") or {})
                    for axis in ("p", "a", "d"):
                        if axis not in value:
                            continue
                        prev = float(base.get(axis, 0.0))
                        target = _clamp(value[axis], -1, 1, prev)
                        if source == "feedback":
                            target = prev + max(-_FEEDBACK_MAX_STEP, min(_FEEDBACK_MAX_STEP, target - prev))
                        base[axis] = round(target, 4)
                        changes.append({"key": f"emotion_baseline.{axis}", "from": prev, "to": base[axis]})
                    new["emotion_baseline"] = base
                    continue
                if key not in DEFAULT_PARAMS:
                    return {"ok": False, "error": f"未知可调参数：{key}"}
                if isinstance(value, bool):
                    new[key] = value
                    changes.append({"key": key, "from": old.get(key), "to": value})
                    continue
                lo, hi = _NUMERIC_BOUNDS.get(key, (0.0, 1.0))
                prev = float(old.get(key, lo))
                target = _clamp(value, lo, hi, prev)
                if source == "feedback" and hi - lo == 1.0:
                    target = prev + max(-_FEEDBACK_MAX_STEP, min(_FEEDBACK_MAX_STEP, target - prev))
                new[key] = round(target, 4) if isinstance(target, float) else int(target)
                changes.append({"key": key, "from": old.get(key), "to": new[key]})
            if not changes:
                return {"ok": False, "error": "没有有效变更"}
            history = list(data.get("history") or [])
            version = len(history) + 1
            history.append({
                "version": version, "params": old, "actor": actor, "reason": reason,
                "source": source, "changes": changes, "ts_ms": _now_ms(),
            })
            data["history"] = history[-_HISTORY_MAX:]
            data["params"] = new
            data["revision"] = int(data.get("revision", 1)) + 1
            self._write(data)
        return {"ok": True, "version": version, "params": new, "changes": changes,
                "source": source, "skeleton_touched": touched_skeleton}

    def rollback(self, version: int) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            row = next((h for h in data.get("history") or [] if h.get("version") == int(version)), None)
            if row is None:
                return {"ok": False, "error": f"版本 {version} 不存在"}
            current = data.get("params") or {}
            history = list(data.get("history") or [])
            history.append({
                "version": len(history) + 1, "params": current, "actor": "frontend",
                "reason": f"回滚到 v{version}", "source": "rollback", "changes": [],
                "ts_ms": _now_ms(),
            })
            data["history"] = history[-_HISTORY_MAX:]
            data["params"] = row.get("params") or DEFAULT_PARAMS
            data["revision"] = int(data.get("revision", 1)) + 1
            self._write(data)
        return {"ok": True, "restored_version": version, "params": data["params"]}

    # ── 反馈 → 提案 → 应用（受控强化回路）──────────────────────────────
    def feedback(self, *, signal: str, valence: float, target: str = "sassiness",
                 note: str = "", session_id: str = "", actor: str = "bot") -> dict[str, Any]:
        """记录一次正/负反馈；满足门槛时生成人格提案（不直接改参数）。

        信号来源：显式（乖/别这样/重喊/爬）、隐式（采纳/重做/打断/回复速度）、
        cognition.pad.p、指令执行成败。复用后端已有 reinforce/supersede 事件语义。
        """
        valence = _clamp(valence, -1, 1, 0.0)
        if abs(valence) < 0.05:
            return {"ok": True, "proposal": None, "note": "中性反馈已记录，不触发演化"}
        target = target if target in DEFAULT_PARAMS else "sassiness"
        with self._lock:
            data = self._read()
            log = list(data.get("feedback_log") or [])
            log.append({
                "id": uuid.uuid4().hex[:12], "signal": str(signal)[:80],
                "valence": valence, "target": target, "note": str(note)[:200],
                "session_id": str(session_id)[:80], "actor": actor, "ts_ms": _now_ms(),
            })
            data["feedback_log"] = log[-_LOG_MAX:]
            # 同向连续 N 次且跨会话 ≥ M 次才生成提案，防止单次情绪冲垮人格
            tail = data["feedback_log"][-_FEEDBACK_STREAK:]
            same = (len(tail) == _FEEDBACK_STREAK
                    and all(x["valence"] > 0 for x in tail) == (valence > 0)
                    and all(x["target"] == target for x in tail)
                    and len({x["session_id"] for x in tail if x["session_id"]}) >= _FEEDBACK_SESSIONS)
            proposal = None
            if same:
                proposals = list(data.get("proposals") or [])
                if not any(x.get("status") == "pending" and x.get("target") == target for x in proposals):
                    current = dict(DEFAULT_PARAMS)
                    current.update(data.get("params") or {})
                    lo, hi = _NUMERIC_BOUNDS.get(target, (0.0, 1.0))
                    step = _FEEDBACK_MAX_STEP if valence > 0 else -_FEEDBACK_MAX_STEP
                    if hi - lo != 1.0:
                        step = step * (hi - lo)
                    proposal = {
                        "id": uuid.uuid4().hex[:12], "target": target,
                        "from": current.get(target),
                        "to": round(max(lo, min(hi, float(current.get(target, lo)) + step)), 4),
                        "direction": "strengthen" if valence > 0 else "weaken",
                        "reason": f"同向连续 {_FEEDBACK_STREAK} 次且跨会话 ≥{_FEEDBACK_SESSIONS}：{signal}",
                        "evidence": [{"signal": x["signal"], "valence": x["valence"],
                                      "session_id": x["session_id"]} for x in tail],
                        "status": "pending", "created_ms": _now_ms(),
                    }
                    proposals.append(proposal)
                    data["proposals"] = proposals[-50:]
            data["revision"] = int(data.get("revision", 1)) + 1
            self._write(data)
        return {"ok": True, "proposal": proposal,
                "note": "已生成人格提案，待前端/主人确认后应用" if proposal else "反馈已累积，未达演化门槛"}

    def proposals(self) -> dict[str, Any]:
        d = self._read()
        rows = d.get("proposals") or []
        return {"ok": True, "items": rows[-30:], "total": len(rows),
                "pending": sum(1 for x in rows if x.get("status") == "pending")}

    def apply_proposal(self, proposal_id: str, *, actor: str = "frontend") -> dict[str, Any]:
        with self._lock:
            d = self._read()
            row = next((x for x in d.get("proposals") or [] if x.get("id") == proposal_id), None)
            if row is None:
                return {"ok": False, "error": "提案不存在"}
            if row.get("status") != "pending":
                return {"ok": False, "error": f"提案状态为 {row.get('status')}，不可重复应用"}
            result = self.set_params({row["target"]: row["to"]}, actor=actor,
                                     reason=f"应用人格提案 {proposal_id}：{row.get('reason', '')}",
                                     source="feedback")
            if result.get("ok"):
                d = self._read()
                for x in d.get("proposals") or []:
                    if x.get("id") == proposal_id:
                        x["status"] = "applied"
                        x["applied_ms"] = _now_ms()
                        x["result_version"] = result.get("version")
                self._write(d)
            return result

    # ── 骨架显式授权编辑（唯一修改入口：前端人格设定页）────────────────
    BASELINE_CONFIRM_PHRASE = "确认修改人格骨架"

    def set_baseline(self, text: str, *, confirm: str = "", actor: str = "frontend") -> dict[str, Any]:
        """主人显式授权的骨架编辑。授权短语不匹配一律拒绝；旧全文自动备份成新档案文档（可回滚）。"""
        if str(confirm).strip() != self.BASELINE_CONFIRM_PHRASE:
            return {"ok": False, "error": "授权短语不匹配：骨架只能由主人在前端显式确认修改"}
        content = str(text).strip()
        if len(content) < 50:
            return {"ok": False, "error": f"骨架全文过短（{len(content)} 字符，需 ≥50），疑似误操作"}
        doc_id = self._read().get("baseline_doc_id", BASELINE_DOC_ID)
        layers = get_context_layers()
        old = layers.documents_read(doc_id).get("content", "")
        backup = layers.documents_mutate(
            action="create",
            title=f"[人格基线] 备份 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            description=f"骨架修改前备份（actor={actor}）",
            content=old,
        )
        backup_id = backup.get("id", "") if isinstance(backup, dict) else ""
        upd = layers.documents_mutate(action="update", doc_id=doc_id, content=content)
        if not (isinstance(upd, dict) and upd.get("ok", True)):
            return {"ok": False, "error": f"骨架写入失败：{upd}"}
        data = self._read()
        data["last_baseline_write"] = {"at_ms": _now_ms(), "actor": actor,
                                       "bytes": len(content.encode("utf-8")), "backup_id": backup_id}
        self._write(data)
        return {"ok": True, "doc_id": doc_id, "bytes": len(content.encode("utf-8")), "backup_id": backup_id}


_PERSONA: PersonaLayer | None = None
_PERSONA_LOCK = threading.Lock()


def get_persona_layer() -> PersonaLayer:
    global _PERSONA
    if _PERSONA is None:
        with _PERSONA_LOCK:
            if _PERSONA is None:
                _PERSONA = PersonaLayer()
    return _PERSONA
