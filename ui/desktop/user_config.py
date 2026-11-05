"""ui.desktop.user_config —— 用户配置持久化 + 预设方案（打包后免重编译）。

- 配置文件：%APPDATA%/super-brain/config.json（Windows）/ ~/.config/super-brain/config.json；
  只存"与出厂默认的差异"（深合并补丁），升级软件后新参数自动落到默认值。
- 绝不存密钥：GitHub PAT 与云端 API key 走系统钥匙串（SettingsStore）。
- 预设：均衡默认 / 性能全开 / 低配省电，应用 = 覆盖一组补丁并立即生效。
- apply_overrides：把补丁写回 BrainConfig 运行期对象（子模型逐字段赋值，pydantic 校验）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from config import BrainConfig

__all__ = [
    "PRESETS",
    "apply_overrides",
    "clear_overrides",
    "config_dir",
    "load_overrides",
    "preset_names",
    "save_overrides",
]


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
    return changed


# ── 预设（"两套默认配置"）────────────────────────────────────────────────
# balanced：出厂默认（清空补丁即回默认，这里显式写关键项，便于用户看到基准）
# performance：全引擎全开 + CUDA 尝试 + 高帧率 + 精排开
# lite：低配设备：关重引擎、低帧率、跳过精排、云 LLM 优先、同步拉长
PRESETS: dict[str, dict[str, dict[str, Any]]] = {
    "balanced": {
        "storage": {"engines_enabled": {
            "relational": True, "vector": True, "graph": True,
            "kv": True, "timeseries": True, "hypergraph": True,
        }},
        "model": {"model_mode": "auto", "use_cuda": False},
        "recall": {"rerank_enabled": True, "top_k": 5},
        "ui": {"timer_interval_ms": 16, "max_labels": 8, "stardust_count": 260},
        "activation": {"max_nodes": 80},
        "sync": {"sync_interval_sec": 300},
    },
    "performance": {
        "storage": {"engines_enabled": {
            "relational": True, "vector": True, "graph": True,
            "kv": True, "timeseries": True, "hypergraph": True,
        }},
        "model": {"model_mode": "auto", "use_cuda": True, "llm_gpu_layers": -1},
        "recall": {"rerank_enabled": True, "top_k": 8, "rrf_k_oversample": 6},
        "ui": {"timer_interval_ms": 16, "max_labels": 14, "stardust_count": 400},
        "activation": {"max_nodes": 140, "max_pulses": 60},
        "sync": {"sync_interval_sec": 120},
    },
    "lite": {
        "storage": {"engines_enabled": {
            "relational": True, "vector": False, "graph": True,
            "kv": True, "timeseries": False, "hypergraph": False,
        }},
        "model": {"model_mode": "cloud", "use_cuda": False},
        "recall": {"rerank_enabled": False, "top_k": 5, "rrf_k_oversample": 3},
        "ui": {"timer_interval_ms": 33, "max_labels": 6, "stardust_count": 120},
        "activation": {"max_nodes": 40, "max_pulses": 20},
        "sync": {"sync_interval_sec": 900},
        "gateway": {"default_rate_limit": 30},
    },
}


def preset_names() -> list[str]:
    return list(PRESETS)


def preset_patch(name: str) -> dict[str, dict[str, Any]]:
    return PRESETS.get(name, {})
