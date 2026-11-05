"""scripts/make_icon.py —— 把设计稿 PNG 转成多尺寸 .ico（窗口图标 + exe 图标）。

    .venv\\Scripts\\python.exe scripts/make_icon.py [源 PNG]

输出 `ui/desktop/assets/app.ico`（16/24/32/48/64/128/256）与 `app.png`（256，托盘/关于页用）。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "ui" / "desktop" / "assets"
# 原始设计稿已归档为 assets/app.png；仓库根的临时文件不再依赖
DEFAULT_SOURCE = ASSETS / "app.png"
SIZES = (16, 24, 32, 48, 64, 128, 256)

__all__ = ["build"]


def build(source: Path | None = None) -> tuple[Path, Path]:
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

    ico = ASSETS / "app.ico"
    square.save(ico, format="ICO", sizes=[(s, s) for s in SIZES])

    png = ASSETS / "app.png"
    square.resize((256, 256), Image.LANCZOS).save(png, format="PNG")
    return ico, png


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    ico, png = build(Path(args[0]) if args else None)
    print(f"图标已生成：{ico} / {png}")
    if not (ASSETS / "app.ico").is_file():
        shutil.copy2(png, ASSETS / "app.ico")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
