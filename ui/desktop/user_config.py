"""兼容层：Qt 前端继续用旧 import 路径，实现已下沉到 `core.user_config`。

删除 Qt 前端时本文件随目录一起移除即可，后端不再依赖它。
"""
from core.user_config import (  # noqa: F401
    PRESETS,
    PRESET_NOTES,
    apply_overrides,
    clear_overrides,
    config_dir,
    load_overrides,
    preset_names,
    preset_patch,
    save_overrides,
)

__all__ = [
    "PRESETS",
    "PRESET_NOTES",
    "apply_overrides",
    "clear_overrides",
    "config_dir",
    "load_overrides",
    "preset_names",
    "preset_patch",
    "save_overrides",
]
