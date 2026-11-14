"""brain.thalamus —— 丘脑：感觉门控与准入控制。

四道闸（架构 §9.1 + 阶段 1 验收"重复写入零冗余"）：
1. 准入    必填/长度前置校验
2. 去重    精确键（内容哈希）+ SimHash 近邻（3-gram，支持中文）
3. 显著性  低显著性只进本地缓冲区，不产生跨设备事件（架构 §8.4 缓冲层不同步）
4. 限流    按 agent_id 滑动窗口计数，防单 Agent 刷爆记忆流

SimHash 自实现（3-gram + blake2b），不引入 simhash 依赖，保证跨设备确定性。
向量近邻去重留接口位：embedder 不可用时跳过，不伪造结果。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from config import BrainConfig, ThalamusConfig, get_config
from core.interfaces import KVRepository
from models.neuron import PAD, Neuron

__all__ = ["Admission", "Thalamus", "classify_intent", "hamming_distance", "simhash64"]

_RATE_BUCKET = "ratelimit"
_DEDUPE_BUCKET = "dedupe"
_RECENT_KEY = "recent"
_EXACT_BUCKET = "dedupe_exact"

# W3 / Q20：同一个 intent 既用于模型路由，也用于召回深度决策，不引入第二套意图分类
_INTENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("deep_recall", ("三个月前", "上次", "之前", "记得那", "回忆", "当时", "那件事")),
    ("media", ("画一张", "发张", "生成图", "来张图", "语音", "配图")),
    ("tool_call", ("调用工具", "执行任务", "运行脚本", "查数据库")),
    ("code", ("def ", "class ", "function ", "代码", "函数", "报错", "traceback")),
    ("emotion", ("难过", "生气", "开心", "焦虑", "心情", "吵架", "委屈")),
)


def _classify_intent_uncached(content: str) -> str:
    for intent, keywords in _INTENT_RULES:
        if any(keyword in content for keyword in keywords):
            return intent
    return "chat"


def classify_intent(text: str) -> str:
    """轻量规则意图判定（聊天热路径 <1ms，不调 LLM）。返回 chat 等 6 类之一。

    v4.1-C 全开：意图分类结果写 ``thalamus`` ns（键=sha256(lower(text))）。
    规则本身 <1ms，缓存收益在**高频重复问法**（同 session 常见寒暄/同指令）与
    统一可观测性；未装配/异常一律回退纯函数，语义不变。
    """
    content = str(text or "").lower()
    try:
        from core.cache_manager import get_cache_manager

        mgr = get_cache_manager()
    except Exception:  # noqa: BLE001
        mgr = None
    key = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if mgr is not None:
        try:
            hit = mgr.get("thalamus", key)
            if isinstance(hit, str):
                return hit
        except Exception:  # noqa: BLE001
            pass
    result = _classify_intent_uncached(content)
    if mgr is not None:
        try:
            mgr.set("thalamus", key, result)
        except Exception:  # noqa: BLE001
            pass
    return result


def _now_ms() -> int:
    return int(time.time() * 1000)


def simhash64(text: str, *, ngram: int = 3) -> int:
    """字符 n-gram SimHash（中文无需分词，英文按字符窗口也足够稳）。"""
    cleaned = " ".join(text.split())
    if not cleaned:
        return 0
    tokens = [cleaned[i : i + ngram] for i in range(max(1, len(cleaned) - ngram + 1))]
    weights = [0] * 64
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        hashed = int.from_bytes(digest, "big")
        for bit in range(64):
            weights[bit] += 1 if (hashed >> bit) & 1 else -1
    result = 0
    for bit, weight in enumerate(weights):
        if weight > 0:
            result |= 1 << bit
    return result


def hamming_distance(left: int, right: int) -> int:
    return bin(left ^ right).count("1")


@dataclass(frozen=True)
class Admission:
    accepted: bool
    reason: str
    significance: float
    simhash: int
    duplicate_of: str | None = None
    buffered_only: bool = False

    @property
    def creates_sync_event(self) -> bool:
        """缓冲层不同步：只有高显著性写入才产生跨设备事件。"""
        return self.accepted and not self.buffered_only


class Thalamus:
    def __init__(self, kv: KVRepository, config: BrainConfig | None = None) -> None:
        self._kv = kv
        self._config = config or get_config()

    @property
    def params(self) -> ThalamusConfig:
        return self._config.thalamus

    # ── 准入 ──────────────────────────────────────────────────────────────
    def admit(self, neuron: Neuron, *, now_ms: int | None = None) -> Admission:
        now = _now_ms() if now_ms is None else int(now_ms)
        content = neuron.content.strip()
        if len(content) < self.params.min_content_chars:
            return Admission(False, f"内容过短（<{self.params.min_content_chars} 字符）", 0.0, 0)

        limit = self.params.rate_limit_per_minute or self._config.gateway.default_rate_limit
        used = self._kv.incr(_RATE_BUCKET, neuron.agent_id, ttl_ms=60_000)
        if used > limit:
            return Admission(False, f"超出限流 {limit}/min", 0.0, 0)

        signature = simhash64(content)

        exact_key = hashlib.sha1(content.encode("utf-8")).hexdigest()
        existing = self._kv.get(_EXACT_BUCKET, exact_key)
        if existing is not None:
            return Admission(False, "精确重复", 0.0, signature, duplicate_of=existing.decode("utf-8"))

        duplicate = self._find_near_duplicate(signature)
        if duplicate is not None:
            return Admission(False, "SimHash 近邻重复", 0.0, signature, duplicate_of=duplicate)

        significance = self._significance(neuron, content)
        self._remember(neuron.uuid7_id, signature, exact_key)

        buffered_only = significance < self.params.significance_threshold
        return Admission(
            accepted=True,
            reason="buffered" if buffered_only else "admitted",
            significance=significance,
            simhash=signature,
            buffered_only=buffered_only,
        )

    def _significance(self, neuron: Neuron, content: str) -> float:
        params = self.params
        # v4.1-C 全开：显著性打分写 ``salience`` ns。键含三权重+PAD+长度+置信度，
        # 参数变了自然换键（无需显式失效）；未装配/异常回退纯计算。
        try:
            from core.cache_manager import get_cache_manager

            mgr = get_cache_manager()
        except Exception:  # noqa: BLE001
            mgr = None
        key = ""
        if mgr is not None:
            p, a, d = neuron.pad.p, neuron.pad.a, neuron.pad.d
            key = hashlib.sha256(
                f"{len(content)}|{p:.3f}|{a:.3f}|{d:.3f}|{neuron.confidence:.3f}"
                f"|{params.significance_length_weight}|{params.significance_pad_weight}"
                f"|{params.significance_confidence_weight}".encode("utf-8")
            ).hexdigest()
            try:
                hit = mgr.get("salience", key)
                if isinstance(hit, (int, float)):
                    return float(hit)
            except Exception:  # noqa: BLE001
                pass
        info = min(1.0, len(content) / max(params.significance_length_ref, 1))
        salience = neuron.pad.salience()
        score = (
            params.significance_length_weight * info
            + params.significance_pad_weight * salience
            + params.significance_confidence_weight * neuron.confidence
        )
        value = max(0.0, min(1.0, score))
        if mgr is not None and key:
            try:
                mgr.set("salience", key, value)
            except Exception:  # noqa: BLE001
                pass
        return value

    # ── 去重索引（KV 单键滚动窗口，确定性、无扫描放大）──────────────────
    def _recent(self) -> list[dict[str, object]]:
        raw = self._kv.get_json(_DEDUPE_BUCKET, _RECENT_KEY)
        return list(raw) if isinstance(raw, list) else []

    def _find_near_duplicate(self, signature: int) -> str | None:
        threshold = self.params.dedupe_hamming_threshold
        for entry in reversed(self._recent()):
            stored = int(entry.get("simhash", 0))
            if hamming_distance(signature, stored) <= threshold:
                return str(entry.get("id", ""))
        return None

    def _remember(self, neuron_id: str, signature: int, exact_key: str) -> None:
        window = max(1, self.params.dedupe_window)
        recent = self._recent()
        recent.append({"id": neuron_id, "simhash": signature})
        self._kv.put_json(_DEDUPE_BUCKET, _RECENT_KEY, recent[-window:])
        self._kv.put(_EXACT_BUCKET, exact_key, neuron_id.encode("utf-8"))

    # ── PAD 便捷入口：便于 router_extractor 直接给三元组 ────────────────
    @staticmethod
    def pad_of(p: float, a: float, d: float) -> PAD:
        return PAD(p=p, a=a, d=d)
