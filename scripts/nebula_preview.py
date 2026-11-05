"""scripts/nebula_preview.py —— 离屏渲染一张"突触激活"预览图（无需显示器）。

    .venv\\Scripts\\python.exe scripts/nebula_preview.py
    .venv\\Scripts\\python.exe scripts/nebula_preview.py --out data/my.png

想要可交互窗口请跑：`python -m ui.desktop`。

预览使用隔离的临时数据目录，不污染 data/ 下的真实库。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6 import QtGui, QtWidgets  # noqa: E402

from config import BrainConfig, ModelConfig, StorageConfig, SyncConfig  # noqa: E402
from ui.desktop.backend import DesktopBackend  # noqa: E402
from ui.desktop.neurons import NebulaView  # noqa: E402

DEMO_MEMORIES = (
    "周五发版出现严重冲突，全员加班到凌晨",
    "发版流程负责人更换为 Bob 并重新评审",
    "架构评审确认缓存分层与向量召回方案",
    "用户偏好直接给出结论而不是铺垫",
    "向量余弦相似度把记忆 A 与记忆 B 连成突触",
    "深夜复盘发现情绪波动与发布事故高度相关",
    "同步平面按设备分目录写事件，天然免冲突",
    "光点沿突触线传导，抵达终点时神经元闪烁",
    "发版冲突复盘：凌晨加班暴露发布流程缺口",
    "余弦相似度高的记忆之间会画一条突触连线",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染突触激活预览图")
    parser.add_argument("--out", default="data/nebula_preview.png")
    args = parser.parse_args()

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    root = Path(tempfile.mkdtemp(prefix="nebula_preview_"))
    config = BrainConfig(
        storage=StorageConfig(
            sqlite_path=root / "r.db", lancedb_path=root / "v.lance",
            kuzu_path=root / "g.kuzu", lmdb_path=root / "kv.lmdb", duckdb_path=root / "t.db",
        ),
        model=ModelConfig(dense_dim=8, use_cuda=False, llm_gguf_dir=root / "models"),
        sync=SyncConfig(device_id="dev-preview", local_repo_dir=root / "sync"),
    )
    # 预览专用：短中文样本的字符 2-gram 余弦偏低，调低阈值好让语义边肉眼可见
    config.activation.similarity_threshold = 0.22
    backend = DesktopBackend(config)
    try:
        for content in DEMO_MEMORIES:
            backend.save_memory(content)
        view = NebulaView(config, auto_animate=False)
        graph = backend.graph()
        view.set_graph(graph)
        view.emit_from_center([(node.id, 0.85) for node in graph.nodes[:5]])
        for _ in range(8):
            view.animate()

        image = QtGui.QImage(1000, 800, QtGui.QImage.Format.Format_ARGB32)
        image.fill(QtGui.QColor(config.ui.background_color))
        painter = QtGui.QPainter(image)
        view.scene.render(painter)
        painter.end()

        out = ROOT / args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        image.save(str(out), "PNG")
        print(f"预览图已生成：{out}")
        print(f"节点 {len(view.nodes)} 个 · 突触线 {len(graph.edges)} 条 · 飞行中光点 {view.pulse_count()} 个")
        print("可交互窗口：python -m ui.desktop")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
