"""scripts/pack_release.py —— 把 onedir 发行包切成 <2GB 的分卷 zip（GitHub Release 上传用）。

GitHub 单文件上限 2GB，而我们的 dist 是 6.3GB（不含模型）。做法：
按**未压缩体积**贪心分组（默认每卷 1.5GB），每卷独立 zip；解压时依次解到同一目录即可。

    python scripts/pack_release.py --dist dist/super-brain --out release --budget-mb 1500

产物：
    release/super-brain-app-part1.zip ... partN.zip
    release/super-brain-app-sha256.txt
    release/install.cmd        （双击即可逐卷解压）
"""
from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path

__all__ = ["main"]

_EXCLUDE_DIRS = {"models"}  # 3.3GB 权重单独分发（或让用户自己下）
_INSTALL_CMD = """@echo off
setlocal
cd /d "%~dp0"
echo 正在逐卷解压到 super-brain\\ ...
if not exist "super-brain" mkdir "super-brain"
for %%f in (super-brain-app-part*.zip) do (
    echo  - %%~nxf
    tar -xf "%%f" -C "super-brain"
    if errorlevel 1 (
        echo [失败] 解压 %%f 出错；请确认 Windows 10 1803+ 自带 tar，或手动用 7-Zip 逐卷解压。
        pause
        exit /b 1
    )
)
echo.
echo 完成：双击 super-brain\\super-brain.exe 启动
pause
"""


def _collect(dist: Path) -> list[Path]:
    files: list[Path] = []
    for path in dist.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _EXCLUDE_DIRS for part in path.relative_to(dist).parts[:-1]):
            continue
        files.append(path)
    return sorted(files, key=lambda p: p.stat().st_size, reverse=True)


def _bins(files: list[Path], budget: int) -> list[list[Path]]:
    buckets: list[list[Path]] = []
    sizes: list[int] = []
    for path in files:
        size = path.stat().st_size
        for index, used in enumerate(sizes):
            if used + size <= budget:
                buckets[index].append(path)
                sizes[index] += size
                break
        else:
            buckets.append([path])
            sizes.append(size)
    return buckets


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", default="dist/super-brain")
    parser.add_argument("--out", default="release")
    parser.add_argument("--budget-mb", type=int, default=1500)
    parser.add_argument("--compress-level", type=int, default=6)
    args = parser.parse_args()

    dist = Path(args.dist)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    files = _collect(dist)
    total = sum(p.stat().st_size for p in files)
    print(f"待打包 {len(files)} 个文件，未压缩 {total / 1024**3:.2f} GB（已排除 models/）")

    buckets = _bins(files, args.budget_mb * 1024 * 1024)
    lines: list[str] = []
    for index, bucket in enumerate(buckets, start=1):
        target = out / f"super-brain-app-part{index}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=args.compress_level) as archive:
            for path in bucket:
                archive.write(path, path.relative_to(dist).as_posix())
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        size_gb = target.stat().st_size / 1024**3
        print(f"  part{index}: {len(bucket)} 文件 → {target.name} {size_gb:.2f} GB")
        lines.append(f"{digest}  {target.name}")

    (out / "super-brain-app-sha256.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "install.cmd").write_text(_INSTALL_CMD, encoding="utf-8")
    print(f"完成：{len(buckets)} 卷 → {out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
