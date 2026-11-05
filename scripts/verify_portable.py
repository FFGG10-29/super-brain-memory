"""scripts/verify_portable.py —— 模拟"装到另一台机器 / 另一个目录"后验证 GPU 仍可调。

    .venv\\Scripts\\python.exe scripts/verify_portable.py

用硬链接（零拷贝）搭出 onedir 布局：

    data/portable_sim/super-brain.exe        (占位)
    data/portable_sim/_internal/cuda_bin/*.dll
    data/portable_sim/_internal/models/...
    data/portable_sim/userdata/              (模拟 %APPDATA% 下的可写数据)
    data/portable_sim/userhome/              (模拟用户配置目录，故意不放 .env)

随后在 import 任何项目模块之前设置 sys.frozen / sys._MEIPASS / sys.executable，
并清掉开发态环境变量，最后跑完整的 --probe-models 流程，要求 verdict == GPU_OK。
这能验证：换目录、换数据目录、没有 .env、DLL 不在 venv 里，显卡依旧能调起来。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "data" / "portable_sim"
INTERNAL = SIM / "_internal"


def main() -> int:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    # 资源归集必须在设置 frozen 之前（此时仍要读 venv 的 site-packages）
    from scripts.build_portable import collect_cuda_dlls, collect_models

    SIM.mkdir(parents=True, exist_ok=True)
    dll_count = collect_cuda_dlls(INTERNAL / "cuda_bin")
    model_count = collect_models(ROOT / "data" / "models", INTERNAL / "models")
    print(f"模拟安装布局：CUDA DLL {dll_count} 个，模型文件 {model_count} 个 → {INTERNAL}")

    # ── 冻结态标记：必须早于 import config / main ────────────────────────
    sys.frozen = True
    sys._MEIPASS = str(INTERNAL)
    sys.executable = str(SIM / "super-brain.exe")
    os.environ["SUPER_BRAIN_HOME"] = str(SIM / "userhome")
    os.environ["SUPER_BRAIN_DATA_DIR"] = str(SIM / "userdata")
    for key in (
        "MODEL_USE_CUDA", "MODEL_LLM_GPU_LAYERS", "MODEL_LLM_GGUF_DIR",
        "SUPER_BRAIN_RESOURCE_DIR", "SUPER_BRAIN_CUDA_DIR", "SUPER_BRAIN_ENV_FILE",
    ):
        os.environ.pop(key, None)

    from core.runtime_paths import data_dir, is_frozen, models_dir, resource_root
    from engine.cuda_runtime import cuda_runtime_status

    print(f"frozen={is_frozen()}  resource={resource_root()}")
    print(f"data={data_dir()}  models={models_dir()}")
    status = cuda_runtime_status()
    print(f"CUDA DLL 齐全={status['dll_complete']} 驱动={status['cuda_driver_api']} "
          f"达标={status['driver_sufficient']} 目录数={len(status['dll_dirs'])}")

    from config import get_config
    print(f"无 .env 时 use_cuda={get_config().model.use_cuda}（应为 True）")

    from main import probe_models
    return probe_models(as_json=True)


if __name__ == "__main__":
    raise SystemExit(main())
