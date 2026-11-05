"""engine.local_llm —— Qwen3-0.6B GGUF（llama.cpp）。

用途：写入流实体抽取（严格 JSON）、分类、PAD 估算、夜间总结。
纪律：
- 输出必须过 JSON 解析 + required_keys 校验，失败重试 max_retries 次后抛 EngineUnavailable，
  由 router_extractor 决定降级（规则抽取 / 拒收），**绝不编造结构化结果**。
- 降级：n_gpu_layers 从配置值递减到 0 再加载（OOM/不支持时）。
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from core.errors import EngineUnavailable
from core.interfaces import LocalLLM

__all__ = ["QwenLocalLLM", "extract_json_object"]

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def extract_json_object(text: str) -> Mapping[str, Any]:
    """从自由文本里抠第一个 JSON 对象（LLM 常在前后加解释或 ```json 围栏）。"""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(candidate)
        if match is None:
            raise ValueError("输出中找不到 JSON 对象")
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, Mapping):
        raise ValueError(f"JSON 顶层必须是对象，实际是 {type(parsed).__name__}")
    return parsed


class QwenLocalLLM(LocalLLM):
    """llama.cpp 封装。懒加载：首次 complete_json 才加载 GGUF。"""

    def __init__(
        self,
        *,
        model_path: str | None = None,
        gpu_layers: int = -1,
        context_size: int = 4096,
        temperature: float = 0.0,
    ) -> None:
        self._model_path = model_path
        self._gpu_layers = int(gpu_layers)
        self._context_size = int(context_size)
        self._temperature = float(temperature)
        self._llm: Any = None
        self._degraded = False
        self._active_gpu_layers = int(gpu_layers)

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def active_gpu_layers(self) -> int:
        return self._active_gpu_layers

    def _ensure_loaded(self) -> Any:
        if self._llm is not None:
            return self._llm
        try:
            from llama_cpp import Llama  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001
            raise EngineUnavailable(f"llama-cpp-python 不可用: {exc}") from exc
        if not self._model_path:
            raise EngineUnavailable("未配置 Qwen3 GGUF 模型路径")

        # n_gpu_layers 递减降级：-1/正数 → 0
        attempts = [self._gpu_layers] if self._gpu_layers == 0 else [self._gpu_layers, 0]
        last_error: Exception | None = None
        for layers in attempts:
            try:
                self._llm = Llama(
                    model_path=self._model_path,
                    n_ctx=self._context_size,
                    n_gpu_layers=layers,
                    verbose=False,
                )
                self._active_gpu_layers = layers
                self._degraded = layers != self._gpu_layers
                return self._llm
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise EngineUnavailable(f"GGUF 加载失败（已尝试 gpu_layers={attempts}）: {last_error}")

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        required_keys: Sequence[str] = (),
        max_retries: int = 2,
    ) -> Mapping[str, Any]:
        llm = self._ensure_loaded()
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            completion = llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=self._temperature,
                response_format={"type": "json_object"},
            )
            text = completion["choices"][0]["message"]["content"]
            try:
                parsed = extract_json_object(text)
                missing = [key for key in required_keys if key not in parsed]
                if missing:
                    raise ValueError(f"缺少必需字段: {missing}")
                return parsed
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                user_prompt = (
                    f"{user_prompt}\n\n上一次输出不合法（{exc}）。只输出合法 JSON 对象，不要解释。"
                )
                _ = attempt
        raise EngineUnavailable(f"LLM 输出在 {max_retries + 1} 次尝试后仍非法: {last_error}")
