"""brain.current_pad_tracker —— 当前会话的实时 PAD 状态（v4.1 W5 / C7 / R4）。

与「记忆自身的 PAD」严格分开：这里表示**用户此刻的情绪状态**，
由最近 N 轮对话的规则估算做低通积分（窗口 5 轮、衰减 0.7），不走 0.6B LLM。

R4 / Q16 分段门控（双权重相加会让「难过时翻旧账更狠」，故改成分段）：
- 冷启动（< warmup_turns 轮）：w6 = 0，没有可靠基线；
- 唤醒度 A < 阈值：w6 正常生效（情绪一致性）；
- A 高且 P 负：w6 = 0，且对 emotion / 负 valence 记忆施加 penalty（召回配额压制）。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from brain.pad_lexicon import estimate_pad
from config import BrainConfig, get_config

__all__ = ["CurrentPadTracker", "PadState"]


@dataclass(frozen=True)
class PadState:
    p: float
    a: float
    d: float

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.p, self.a, self.d)

    def similarity(self, other: tuple[float, float, float]) -> float:
        """1 - 归一化曼哈顿距离；取值 [0, 1]。"""
        distance = (abs(self.p - other[0]) + abs(self.a - other[1])
                    + abs(self.d - other[2]))
        return max(0.0, 1.0 - distance / 6.0)


class CurrentPadTracker:
    def __init__(self, config: BrainConfig | None = None, *,
                 window: int = 5, decay: float = 0.7) -> None:
        self._config = config or get_config()
        self._history: deque[PadState] = deque(maxlen=max(1, window))
        self._decay = max(0.0, min(1.0, decay))
        self.turns = 0

    @property
    def gating(self):
        return self._config.pad_gating

    def observe(self, text: str) -> PadState:
        """喂入一轮用户消息，返回低通积分后的当前 PAD。"""
        p, a, d = estimate_pad(text)
        if self._history:
            previous = self._history[-1]
            p = self._decay * previous.p + (1 - self._decay) * p
            a = self._decay * previous.a + (1 - self._decay) * a
            d = self._decay * previous.d + (1 - self._decay) * d
        state = PadState(round(p, 3), round(a, 3), round(d, 3))
        self._history.append(state)
        self.turns += 1
        return state

    def current(self) -> PadState | None:
        return self._history[-1] if self._history else None

    @property
    def warmup_done(self) -> bool:
        return self.turns >= int(self.gating.warmup_turns)

    @property
    def suppress_negative(self) -> bool:
        """A 高且 P 负 → 压制负面记忆的召回配额。"""
        state = self.current()
        if state is None or not self.warmup_done:
            return False
        return state.a >= float(self.gating.arousal_threshold) and state.p < -0.15

    def weight_multiplier(self) -> float:
        """分段：冷启动/高负唤醒返回 0，其余返回 1。"""
        if not self.gating.enabled:
            return 0.0
        if not self.warmup_done or self.suppress_negative:
            return 0.0
        return 1.0
