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

__all__ = ["Admission", "Thalamus", "hamming_distance", "simhash64"]

_RATE_BUCKET = "ratelimit"
_DEDUPE_BUCKET = "dedupe"
_RECENT_KEY = "recent"
_EXACT_BUCKET = "dedupe_exact"


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
        info = min(1.0, len(content) / max(params.significance_length_ref, 1))
        salience = neuron.pad.salience()
        score = (
            params.significance_length_weight * info
            + params.significance_pad_weight * salience
            + params.significance_confidence_weight * neuron.confidence
        )
        return max(0.0, min(1.0, score))

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
