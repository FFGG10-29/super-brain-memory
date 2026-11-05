"""eval.golden_set —— recall@k / MRR / 各路召回命中率基线。

样例用「查询 + 相关正文关键词」定义，不绑定运行时 UUID —— 换模型/换权重可直接重跑。
评判口径唯一：RRF 权重与衰减系数网格搜索、换模型对比都以本模块输出为准。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brain.prefrontal import Prefrontal

__all__ = ["GoldenCase", "SAMPLE_CASES", "compare", "evaluate", "load_baseline", "save_baseline"]


@dataclass(frozen=True)
class GoldenCase:
    query: str
    relevant: tuple[str, ...]
    note: str = ""


SAMPLE_CASES: tuple[GoldenCase, ...] = (
    GoldenCase("周五发版冲突", ("发版", "冲突"), "情绪/情景记忆"),
    GoldenCase("发布流程负责人是谁", ("负责人", "owner"), "事实演进与 supersede"),
    GoldenCase("用户偏好什么沟通方式", ("偏好", "直接"), "画像槽位"),
    GoldenCase("上次架构评审结论", ("架构评审", "结论"), "时间检索"),
    GoldenCase("代码报错怎么修", ("报错", "编译", "bug"), "代码记忆"),
)


def _is_relevant(content: str, relevant: tuple[str, ...]) -> bool:
    return any(keyword and keyword in content for keyword in relevant)


def evaluate(
    prefrontal: Prefrontal,
    cases: tuple[GoldenCase, ...] = SAMPLE_CASES,
    *,
    agent_id: str = "eval",
    k: int = 5,
) -> dict[str, Any]:
    recall_scores: list[float] = []
    reciprocal_ranks: list[float] = []
    channel_counts: dict[str, int] = {}
    per_case: list[dict[str, Any]] = []

    for case in cases:
        hits = prefrontal.recall(case.query, agent_id=agent_id, limit=k)
        contents = [hit.neuron.content for hit in hits]
        matched = sum(1 for content in contents if _is_relevant(content, case.relevant))
        recall_scores.append(matched / max(len(case.relevant), 1))
        rank = next(
            (index + 1 for index, content in enumerate(contents) if _is_relevant(content, case.relevant)),
            0,
        )
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)
        for hit in hits:
            for channel in hit.channels:
                channel_counts[channel] = channel_counts.get(channel, 0) + 1
        per_case.append({"query": case.query, "hits": len(hits), "matched": matched, "rank": rank})

    total_hits = sum(channel_counts.values()) or 1
    return {
        f"recall@{k}": round(sum(recall_scores) / max(len(recall_scores), 1), 4),
        "mrr": round(sum(reciprocal_ranks) / max(len(reciprocal_ranks), 1), 4),
        "cases": len(cases),
        "channel_hit_rate": {key: round(value / total_hits, 4) for key, value in sorted(channel_counts.items())},
        "per_case": per_case,
    }


def save_baseline(path: str | Path, metrics: dict[str, Any], *, label: str = "baseline") -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"label": label, "metrics": metrics}, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return target


def load_baseline(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["metrics"]


def compare(baseline: dict[str, Any], current: dict[str, Any], *, metrics: tuple[str, ...] = ("recall@5", "mrr")) -> dict:
    """换模型/调参后的对比报告：正数为提升。"""
    report: dict[str, Any] = {}
    for metric in metrics:
        before, after = float(baseline.get(metric, 0.0)), float(current.get(metric, 0.0))
        report[metric] = {"before": before, "after": after, "delta": round(after - before, 4)}
    return report
