"""阶段 0 基线自检：一条命令确认「venv + 六引擎包 + 冷启动 + 降级」都成立。

用法（项目根执行）：
    .venv\\Scripts\\python.exe scripts/verify_baseline.py

检查项：
    1. 解释器确实在 .venv 内（系统 python 缺存储依赖）
    2. requirements.txt 里每个 == 锁定的版本与已安装版本一致
    3. 六引擎在隔离临时目录建表 + health() 全 OK
    4. build_app 冷启动 < 30s（架构阶段 0 验收）
    5. 断 CUDA 落 CPU 的降级解析正确、UUIDv7/HLC 基本性质成立

退出码 0 = 基线成立；1 = 至少一项硬错误。
"""
from __future__ import annotations

import importlib.metadata as metadata
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

# 允许缺失的包（代码有确定性降级路径，不算基线破坏）
SOFT_REQUIREMENTS = {"hypernetx"}

errors: list[str] = []
warnings: list[str] = []
checks: list[tuple[str, str]] = []


def ok(name: str, detail: str = "") -> None:
    checks.append(("OK", f"{name} {detail}"))


def fail(name: str, detail: str) -> None:
    errors.append(f"{name}: {detail}")
    checks.append(("FAIL", f"{name} {detail}"))


def warn(name: str, detail: str) -> None:
    warnings.append(f"{name}: {detail}")
    checks.append(("WARN", f"{name} {detail}"))


def check_interpreter() -> None:
    venv = (ROOT / ".venv").resolve()
    try:
        Path(sys.executable).resolve().relative_to(venv)
        ok("解释器", f"{sys.executable} (Python {sys.version.split()[0]})")
    except ValueError:
        warn("解释器", f"当前不在项目 .venv 内：{sys.executable}")


def check_packages() -> None:
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "==" not in line:
            continue
        name, expected = (part.strip() for part in line.split("==", 1))
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            if name in SOFT_REQUIREMENTS:
                warn("依赖", f"{name} 未安装（走内置降级路径）")
            else:
                fail("依赖", f"{name} 未安装，期望 {expected}")
            continue
        if installed != expected:
            fail("依赖", f"{name} 版本 {installed} != 锁定 {expected}")
        else:
            ok("依赖", f"{name}=={installed}")


def build_config(root: Path):
    from config import BrainConfig, ModelConfig, StorageConfig, SyncConfig

    return BrainConfig(
        storage=StorageConfig(
            sqlite_path=root / "relational.db",
            lancedb_path=root / "vector.lance",
            kuzu_path=root / "graph.kuzu",
            lmdb_path=root / "kv.lmdb",
            duckdb_path=root / "timeseries.db",
        ),
        model=ModelConfig(dense_dim=1024, use_cuda=False, llm_gguf_dir=root / "models"),
        sync=SyncConfig(device_id="dev-baseline", local_repo_dir=root / "brain-sync"),
    )


def check_engines(root: Path) -> None:
    from main import build_storage

    config = build_config(root)
    engines = build_storage(config)
    try:
        for engine in engines.all():
            status = engine.health()
            if status.ok:
                ok("引擎", f"{status.engine} {status.latency_ms:.2f}ms {status.detail}")
            else:
                fail("引擎", f"{status.engine} {status.detail}")
    finally:
        engines.close()


def check_cold_start(root: Path) -> None:
    from main import build_app

    config = build_config(root)
    started = time.perf_counter()
    app = build_app(config)
    elapsed = time.perf_counter() - started
    try:
        if elapsed >= 30.0:
            fail("冷启动", f"{elapsed:.2f}s 超出 30s 预算")
        else:
            ok("冷启动", f"{elapsed:.2f}s / 预算 30s")
        unhealthy = [status.engine for status in app.health_report() if not status.ok]
        if unhealthy:
            fail("组合根", f"健康检查不通过: {unhealthy}")
        else:
            ok("组合根", "health_report 全 OK")
    finally:
        app.close()


def check_fallbacks() -> None:
    from core.ids import HybridLogicalClock, new_uuid7
    from engine.embedding import resolve_onnx_providers

    if resolve_onnx_providers(True, ["CPUExecutionProvider"]) != ["CPUExecutionProvider"]:
        fail("降级", "断 CUDA 未落 CPU")
    else:
        ok("降级", "断 CUDA → CPUExecutionProvider")

    identifier = uuid.UUID(new_uuid7())
    if identifier.version != 7:
        fail("时钟", "UUIDv7 版本位错误")
    else:
        ticks = iter([1000, 999, 998, 997])
        clock = HybridLogicalClock("dev-baseline", now_ms=lambda: next(ticks))
        stamps = [clock.tick() for _ in range(3)]
        if stamps == sorted(stamps) and len({s.to_token() for s in stamps}) == 3:
            ok("时钟", "UUIDv7 + HLC 回拨单调")
        else:
            fail("时钟", "HLC 单调性被破坏")


def main() -> int:
    print("── 阶段 0 基线自检 ──────────────────────────────────────────")
    check_interpreter()
    check_packages()
    # 刻意用 mkdtemp 且不做递归删除：本机 safe-delete 钩子会拦截 shutil.rmtree
    root = Path(tempfile.mkdtemp(prefix="brain_baseline_"))
    try:
        check_engines(root)
        check_cold_start(root)
    except Exception as exc:  # noqa: BLE001  自检脚本要报告而非抛栈
        fail("引擎/组合根", f"{type(exc).__name__}: {exc}")
    check_fallbacks()

    for state, line in checks:
        print(f"  [{state:4}] {line}")
    print(f"── 临时数据目录：{root}（自检产物，不入库）")
    if warnings:
        print(f"警告 {len(warnings)} 条：" + "; ".join(warnings))
    if errors:
        print(f"失败 {len(errors)} 条：" + "; ".join(errors))
        return 1
    print("基线成立：六引擎连通 / 版本一致 / 冷启动达标 / 降级正确")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
