"""core.user_config —— 用户配置持久化 + 预设方案（与 UI 框架无关）。

- 配置文件：%APPDATA%/super-brain/config.json（Windows）/ ~/.config/super-brain/config.json；
  只存"与出厂默认的差异"（深合并补丁），升级软件后新参数自动落到默认值。
- 绝不存密钥：GitHub PAT 与云端 API key 走系统钥匙串。
- 预设：均衡默认 / 核显省电 / 独显性能，应用 = 覆盖一组补丁并立即生效。
- apply_overrides：把补丁写回 BrainConfig 运行期对象（子模型逐字段赋值）。

本模块为无头后端 / Flutter RPC 提供用户配置读写能力；
配置能力完全由 core.* 承接，不依赖任何 UI 框架模块。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from config import BrainConfig

__all__ = [
    "PRESETS",
    "PRESET_NOTES",
    "apply_overrides",
    "clear_overrides",
    "config_dir",
    "load_overrides",
    "on_change",
    "preset_names",
    "preset_patch",
    "save_overrides",
]

# C8 / R9：不用文件 mtime 轮询，配置变更由 apply_overrides / save_overrides 主动通知订阅者。
# 订阅者回调签名 (patches: dict) -> None，必须幂等（同一补丁可能被通知两次）。
_LISTENERS: list[Any] = []


def on_change(callback: Any) -> None:
    """注册配置变更回调；model_router 用它热重载供应商池与 intent_map。"""
    if callable(callback) and callback not in _LISTENERS:
        _LISTENERS.append(callback)


def _notify(patches: dict[str, Any]) -> None:
    for callback in list(_LISTENERS):
        try:
            callback(patches)
        except Exception:  # noqa: BLE001  单个订阅者失败不影响配置生效
            pass


def config_dir() -> Path:
    # 测试/便携模式可用 SUPER_BRAIN_CONFIG_DIR 重定向，避免污染真实用户目录
    override = os.environ.get("SUPER_BRAIN_CONFIG_DIR")
    if override:
        path = Path(override)
    else:
        base = os.environ.get("APPDATA") or str(Path.home() / ".config")
        path = Path(base) / "super-brain"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _config_file() -> Path:
    return config_dir() / "config.json"


def load_overrides() -> dict[str, Any]:
    try:
        return dict(json.loads(_config_file().read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}


def save_overrides(patches: dict[str, Any]) -> dict[str, Any]:
    """深合并写盘，返回合并后的全量补丁。Path 等对象序列化为字符串。"""
    merged = _deep_merge(load_overrides(), patches)
    _config_file().write_text(
        json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _notify(patches)
    return merged


def clear_overrides() -> None:
    try:
        _config_file().unlink()
    except OSError:
        pass


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(dict(result[key]), value)
        else:
            result[key] = value
    return result


def _coerce(section: Any, field_name: str, value: Any) -> Any:
    """按字段注解把 JSON 值转回 Python 类型（Path 字段最关键：字符串要还原成 Path）。"""
    from pydantic import BaseModel

    if not isinstance(section, BaseModel):
        return value
    field = type(section).model_fields.get(field_name)
    if field is None:
        return value
    if field.annotation is Path:
        return Path(str(value))
    return value


def apply_overrides(config: BrainConfig, patches: dict[str, Any]) -> list[str]:
    """把补丁写回运行期 config；返回被改动的 "section.field" 列表。未知字段忽略并跳过。"""
    changed: list[str] = []
    for section_name, section_patch in patches.items():
        section = getattr(config, section_name, None)
        if section is None or not isinstance(section_patch, dict):
            continue
        for field_name, value in section_patch.items():
            if not hasattr(section, field_name):
                continue
            setattr(section, field_name, _coerce(section, field_name, value))
            changed.append(f"{section_name}.{field_name}")
    if changed:
        _notify(patches)
    return changed


# ── 预设（按硬件形态）────────────────────────────────────────────────────
PRESETS: dict[str, dict[str, dict[str, Any]]] = {
    "balanced": {
        "storage": {"engines_enabled": {
            "relational": True, "vector": True, "graph": True,
            "kv": True, "timeseries": True, "hypergraph": True,
        }},
        "model": {"embed_backend": "local", "llm_backend": "local", "use_cuda": False, "llm_gpu_layers": -1},
        "recall": {"rerank_enabled": True, "top_k": 5, "rrf_k_oversample": 4},
        "ui": {"timer_interval_ms": 16, "max_labels": 8, "stardust_count": 260},
        "activation": {"max_nodes": 80, "max_pulses": 40},
        "sync": {"sync_interval_sec": 300},
    },
    "igpu": {
        "storage": {"engines_enabled": {
            "relational": True, "vector": True, "graph": True,
            "kv": True, "timeseries": True, "hypergraph": True,
        }},
        "model": {"embed_backend": "local", "llm_backend": "cloud", "use_cuda": False, "llm_gpu_layers": 0},
        "recall": {"rerank_enabled": False, "top_k": 5, "rrf_k_oversample": 3},
        "ui": {"timer_interval_ms": 33, "max_labels": 6, "stardust_count": 120},
        "activation": {"max_nodes": 40, "max_pulses": 20},
        "sync": {"sync_interval_sec": 900},
        "gateway": {"default_rate_limit": 30},
    },
    "dgpu": {
        "storage": {"engines_enabled": {
            "relational": True, "vector": True, "graph": True,
            "kv": True, "timeseries": True, "hypergraph": True,
        }},
        "model": {"embed_backend": "local", "llm_backend": "local", "use_cuda": True, "llm_gpu_layers": -1},
        "recall": {"rerank_enabled": True, "top_k": 8, "rrf_k_oversample": 6},
        "ui": {"timer_interval_ms": 16, "max_labels": 14, "stardust_count": 400},
        "activation": {"max_nodes": 140, "max_pulses": 60},
        "sync": {"sync_interval_sec": 120},
    },
}

PRESET_NOTES: dict[str, str] = {
    "balanced": (
        "出厂推荐（中肯基准）：六引擎全开、CPU 推理、60fps、精排开。\n"
        "适合大多数设备；不确定选哪个就用它，之后可在下方任意微调。"
    ),
    "igpu": (
        "核显 / 无独显笔记本：本地 LLM 完全走 CPU、对话模型优先云端、关闭精排、\n"
        "33fps 省电、星云节点减半、同步放缓。功能完整，只是更保守省电。"
    ),
    "dgpu": (
        "独显（NVIDIA）：CUDA 加速、本地 LLM 全部层上 GPU、60fps、精排开、\n"
        "星云节点更多、召回更宽、同步更频繁。效果最好，功耗最高。"
    ),
}


def preset_names() -> list[str]:
    return list(PRESETS)


def preset_patch(name: str) -> dict[str, dict[str, Any]]:
    return PRESETS.get(name, {})
