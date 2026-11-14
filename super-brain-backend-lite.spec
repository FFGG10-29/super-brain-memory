# -*- mode: python -*-
# 2026-11-14 前后端合一安装包专用：精简 frozen 后端（onedir）。
# 云端化后本地推理运行时（onnxruntime 2.5G / llama_cpp 1.4G / CUDA DLL 2G+）
# 全部不再需要：项目里这些 import 都是函数内懒加载且带 try/except 兜底，
# scripts/test_lite_backend.py 已验证精简包可启动。故排除后直接产出小体积包，
# 不再依赖 Inno Setup 的 Excludes 去挡 6.2G 死重。
from pathlib import Path

a = Analysis(
    [str(Path(SPECPATH) / "backend_entry.py")],
    pathex=[SPECPATH],
    binaries=[],
    datas=[],
    hiddenimports=[
        "tokenizers",
        "kuzu", "lancedb", "pyarrow", "duckdb", "lmdb", "hypernetx",
        "mcp", "dotenv", "pydantic_settings", "websockets", "honker",
        "main", "headless_server", "v3.daemon", "v3.worker", "v3.rpc", "v3.rpc_ext",
        "brain.memory_service", "brain.memory_editor", "brain.memory_consolidator", "brain.activation",
        "workers.life_loop", "workers.circadian", "sync.git_worker", "sync.snapshot",
        "gateway.mcp_stdio", "gateway.http_adapter", "gateway.mcp_server",
        "gateway.tool_catalog", "gateway.bridge_launcher",
        # 函数内延迟导入，PyInstaller 静态分析抓不到
        "core.context_layers", "core.fast_recall", "core.wal_maintenance",
        "core.structured_log", "core.runtime_paths", "core.user_config",
        "core.system_stats", "core.desktop_features", "core.win32_tray",
        "psutil", "winreg",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "pytest", "IPython",
        "onnxruntime", "llama_cpp", "torch", "torchvision", "triton",
        "nvidia", "tensorrt", "cupy", "pycuda",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=None)
# console=False：前端拉起时不会有命令行黑窗；CLI 版仅排障用
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="headless-server", console=False)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="headless-server-cli", console=True)
coll = COLLECT(
    exe, cli, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, name="super-brain-backend",
)
