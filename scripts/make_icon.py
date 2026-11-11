"""scripts/make_icon.py —— 把设计稿 PNG 转成多尺寸 .ico（窗口图标 + exe 图标）。

    .venv\\Scripts\\python.exe scripts/make_icon.py [源 PNG]

输出 Flutter 安装包图标 `flutter_app/windows/runner/resources/app_icon.ico`（16/24/32/48/64/128/256）。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "flutter_app" / "windows" / "runner" / "resources"
# 设计稿 PNG 需显式传入；仓库不再保留历史图标资源
DEFAULT_SOURCE = None
SIZES = (16, 24, 32, 48, 64, 128, 256)

__all__ = ["build"]


def build(source: Path | None = None) -> Path:
    src = source or DEFAULT_SOURCE
    if not src.is_file():
        raise FileNotFoundError(f"找不到图标源文件：{src}")
    ASSETS.mkdir(parents=True, exist_ok=True)
    image = Image.open(src).convert("RGBA")
    # 方形画布：非正方形先居中裁切，避免 ico 拉伸变形
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    square = image.crop((left, top, left + side, top + side))

    ico = ASSETS / "app_icon.ico"
    square.save(ico, format="ICO", sizes=[(s, s) for s in SIZES])
    return ico


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    src = Path(args[0]) if args else None
    if src is None:
        raise SystemExit("用法：scripts/make_icon.py <设计稿 PNG>")
    ico = build(src)
    print(f"图标已生成：{ico}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
