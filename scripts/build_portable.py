"""scripts/build_portable.py —— 准备可移植的 onedir 发行布局。

    .venv\\Scripts\\python.exe scripts/build_portable.py
    .venv\\Scripts\\python.exe scripts/build_portable.py --no-models      # 模型由用户自行放置

做两件事：
1. 把 venv 里分散在 llama_cpp/lib、onnxruntime/capi、nvidia/*/bin 的 CUDA 运行库
   按文件名去重，归集到 build/portable_stage/cuda_bin/（同卷优先硬链接，零拷贝）。
2. 把模型权重归集到 build/portable_stage/models/。
3. 生成 PyInstaller onedir spec；冻结态下 core.runtime_paths 会把
   sys._MEIPASS 当作资源根，于是 cuda_bin/ 与 models/ 正好落在查找路径上。

随后执行：.venv\\Scripts\\python.exe -m PyInstaller super-brain-backend.spec --noconfirm
发行目录 dist/super-brain-backend/ 里，可写数据自动落到 %APPDATA%/super-brain/data。
（本脚本只生成后端专用 onedir 构建，随 Flutter 安装包部署。）
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "build" / "portable_stage"
CUDA_OUT = STAGE / "cuda_bin"
MODEL_OUT = STAGE / "models"

__all__ = ["collect_cuda_dlls", "collect_models"]


def _link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    try:
        os.link(src, dst)          # 同卷：零拷贝，3~4GB 也瞬间完成
    except OSError:
        shutil.copy2(src, dst)


def _cuda_source_dirs() -> list[Path]:
    site = Path(sysconfig.get_paths().get("purelib", ""))
    dirs = [site / "llama_cpp" / "lib", site / "onnxruntime" / "capi"]
    nvidia = site / "nvidia"
    if nvidia.is_dir():
        dirs.extend(p / "bin" for p in sorted(nvidia.iterdir()) if (p / "bin").is_dir())
    return [p for p in dirs if p.is_dir()]


def collect_cuda_dlls(out: Path | None = None) -> int:
    """按文件名去重归集 CUDA DLL，返回文件数。"""
    target = out or CUDA_OUT
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    for source in _cuda_source_dirs():
        for dll in source.glob("*.dll"):
            _link_or_copy(dll, target / dll.name)
            count = len(list(target.glob("*.dll")))
    return count


def collect_models(source: Path | None = None, out: Path | None = None) -> int:
    src = source or ROOT / "data" / "models"
    target = out or MODEL_OUT
    if not src.is_dir():
        return 0
    count = 0
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        if item.is_dir():
            (target / rel).mkdir(parents=True, exist_ok=True)
        else:
            _link_or_copy(item, target / rel)
            count += 1
    return count




# ── 后端专用冻结模板（--headless）：无 GUI，随 Flutter 安装包部署 ──
_SPEC_BACKEND = '''# -*- mode: python -*-
# 由 scripts/build_portable.py --headless 生成；onedir。
from pathlib import Path

stage = Path(SPECPATH) / "build" / "portable_stage"
cuda_bin = stage / "cuda_bin"
models = stage / "models"

binaries = [(str(p), "cuda_bin") for p in sorted(cuda_bin.glob("*.dll"))]
try:
    import llama_cpp as _llama_cpp
    _llama_lib = Path(_llama_cpp.__file__).parent / "lib"
except Exception:
    _llama_lib = None
if _llama_lib is not None and _llama_lib.is_dir():
    binaries += [(str(p), "llama_cpp/lib") for p in sorted(_llama_lib.glob("*.dll"))]

INCLUDE_MODELS = False  # 模型权重不进冻结包，统一由 --link-models 后处理硬链接
datas = [(str(models), "models")] if (INCLUDE_MODELS and models.is_dir()) else []

a = Analysis(
    [str(Path(SPECPATH) / "backend_entry.py")],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "llama_cpp", "onnxruntime", "onnxruntime.capi._pybind_state", "tokenizers",
        "kuzu", "lancedb", "pyarrow", "duckdb", "lmdb", "hypernetx",
        "mcp", "dotenv", "pydantic_settings", "websockets", "honker",
        "main", "headless_server", "v3.daemon", "v3.worker", "v3.rpc", "v3.rpc_ext",
        "brain.memory_service", "brain.memory_editor", "brain.memory_consolidator", "brain.activation",
        "workers.life_loop", "workers.circadian", "sync.git_worker", "sync.snapshot",
        "gateway.mcp_stdio", "gateway.http_adapter", "gateway.mcp_server",
        "gateway.tool_catalog", "gateway.bridge_launcher",
        # 函数内延迟导入，PyInstaller 静态分析抓不到，冻结包会 ModuleNotFoundError
        "core.context_layers", "core.fast_recall", "core.wal_maintenance",
        "core.structured_log", "core.runtime_paths", "core.user_config",
        # Flutter 桌面能力已下沉到 core（函数内惰性 import，静态分析会漏）
        "core.system_stats", "core.desktop_features", "core.win32_tray",
        # psutil 是 CPU/内存仪表的硬依赖，绝不能 exclude
        "psutil", "winreg",
    ],
    hookspath=[],
    runtime_hooks=[],
    # keyring 是「桌面能力」页 SecretStore 的运行时依赖，必须保留（不能 exclude）。
    # 仅排除真正不会用到的重型 / 测试库以缩小体积。
    excludes=["tkinter", "matplotlib", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=None)
# console=True 的 CLI 版便于排障；两者都是无界面的后端进程
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="headless-server", console=False)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="headless-server-cli", console=True)
coll = COLLECT(
    exe, cli, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, name="super-brain-backend",
)
'''

_ENTRY_BACKEND = '''"""PyInstaller 冻结入口：无参数启动无头后端；带 --self-check / --probe-models 走 CLI。"""
from __future__ import annotations

import sys


def _cli(argv: list[str]) -> int | None:
    as_json = "--json" in argv
    import main as cli

    if "--probe-models" in argv:
        return cli.probe_models(as_json=as_json)
    if "--self-check" in argv:
        return cli.self_check(as_json=as_json)
    if "--mcp-stdio" in argv:
        from gateway.mcp_stdio import main as mcp_main

        return mcp_main()
    return None


def main() -> int:
    code = _cli(sys.argv[1:])
    if code is not None:
        return code
    from headless_server import main as serve_main

    return serve_main(sys.argv[1:])


raise SystemExit(main())
'''


def trim_dist(dist: Path) -> tuple[int, int]:
    """去掉 PyInstaller 打包后的冗余大文件，返回 (删除文件数, 释放 MB)。

    两类冗余：
    1. cuda_bin/ 里与 onnxruntime/capi、llama_cpp/lib 同名的 DLL —— 构建脚本为了让
       「冻结态一定能找到 CUDA 库」做了平铺归集，但这两个包目录本身已被 PyInstaller 收进去，
       且 engine.cuda_runtime 会把它们一并登记进 DLL 搜索路径，所以平铺的那份纯属重复（约 3.4GB）。
    2. onnxruntime_providers_tensorrt.dll —— 我们从不在 providers 里请求 TensorRT，
       留着只是让 onnxruntime 启动时探测（本项目 CUDA/CPU 两档足够）。
    """
    internal = dist / "_internal"
    if not internal.is_dir():
        return 0, 0
    keep_dirs = [internal / "onnxruntime" / "capi", internal / "llama_cpp" / "lib"]
    kept = {p.name for directory in keep_dirs if directory.is_dir() for p in directory.glob("*.dll")}

    removed = 0
    freed = 0
    cuda_bin = internal / "cuda_bin"
    if cuda_bin.is_dir():
        for dll in list(cuda_bin.glob("*.dll")):
            if dll.name in kept:
                freed += dll.stat().st_size
                dll.unlink()
                removed += 1

    for pattern in ("onnxruntime_providers_tensorrt.dll", "onnxruntime_providers_tensorrt*.dll"):
        for target in internal.rglob(pattern):
            if target.is_file():
                freed += target.stat().st_size
                target.unlink()
                removed += 1
    return removed, int(freed / (1024 * 1024))


def link_models_into(dist: Path) -> int:
    """把 build/portable_stage/models 硬链接进 dist/_internal/models。

    PyInstaller 的 datas 是「逐个复制」：3GB 权重会被复制一份，构建慢且白占空间。
    同卷硬链接零拷贝，且对外仍是普通文件（整个 dist 目录拷到别的机器照样能用）。
    """
    internal = dist / "_internal"
    target = internal / "models"
    if not MODEL_OUT.is_dir():
        return 0
    if not internal.is_dir():
        print(f"跳过：{internal} 不存在（先跑 PyInstaller）")
        return 0
    count = 0
    for item in MODEL_OUT.rglob("*"):
        rel = item.relative_to(MODEL_OUT)
        if item.is_dir():
            (target / rel).mkdir(parents=True, exist_ok=True)
        else:
            _link_or_copy(item, target / rel)
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="准备可移植 onedir 后端发行布局（随 Flutter 部署）")
    parser.add_argument("--no-models", action="store_true", help="不归集模型权重（用户自行放置）")
    parser.add_argument(
        "--models-in-spec", action="store_true",
        help="让 PyInstaller 复制模型（默认改为构建后硬链接到 _internal/models，省 3GB 与数分钟）",
    )
    parser.add_argument("--link-models", metavar="DIST", default="",
                        help="PyInstaller 完成后调用：把模型硬链接进 DIST/_internal/models")
    parser.add_argument("--trim", metavar="DIST", default="",
                        help="PyInstaller 完成后调用：删掉重复 CUDA DLL 与 TensorRT provider")
    parser.add_argument("--out", default="dist/super-brain-backend", help="发行目录名")
    parser.add_argument("--headless", action="store_true",
                        help="（已默认）只生成后端专用 spec/入口，无桌面 UI")
    args = parser.parse_args()

    if args.trim:
        removed, freed = trim_dist(Path(args.trim))
        print(f"瘦身：删除 {removed} 个文件，释放 {freed} MB")
        return 0

    if args.link_models:
        count = link_models_into(Path(args.link_models))
        print(f"模型硬链接：{count} 个 → {Path(args.link_models) / '_internal' / 'models'}")
        return 0

    dll_count = collect_cuda_dlls()
    model_count = 0 if args.no_models else collect_models()
    print(f"CUDA DLL 归集：{dll_count} 个 → {CUDA_OUT}")
    print(f"模型权重归集：{model_count} 个 → {MODEL_OUT}")

    # 仅后端构建：不再生成/覆盖 app_entry.py。
    out = args.out
    entry = ROOT / "backend_entry.py"
    entry.write_text(_ENTRY_BACKEND, encoding="utf-8")
    spec = ROOT / "super-brain-backend.spec"
    spec.write_text(_SPEC_BACKEND, encoding="utf-8")
    print(f"已生成 {spec.name}（后端专用：无 GUI 资源）；执行：")
    print("  .venv\\Scripts\\python.exe -m PyInstaller super-brain-backend.spec --noconfirm")
    print(f"  .venv\\Scripts\\python.exe scripts/build_portable.py --trim {out}")
    if not args.models_in_spec:
        print(f"  .venv\\Scripts\\python.exe scripts/build_portable.py --link-models {out}")
    print(f"产物 {out}\\headless-server.exe；由 Flutter 自动拉起，目标机器无需 Python")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
