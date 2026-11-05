"""scripts/desktop_screenshots.py —— 离屏遍历各功能页并截图（无需显示器）。

    .venv\\Scripts\\python.exe scripts/desktop_screenshots.py

输出到 data/screenshots/*.png；可交互窗口请跑 `python -m ui.desktop`。
"""
from __future__ import annotations

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
from ui.desktop.main_window import MainWindow  # noqa: E402
from ui.desktop.settings import SettingsStore  # noqa: E402
from scripts.nebula_preview import _MemorySettings, _NoKeyring  # noqa: E402

DEMO_MEMORIES = (
    "周五发版出现严重冲突，全员加班到凌晨",
    "发版流程负责人更换为 Bob 并重新评审",
    "发版冲突复盘：凌晨加班暴露发布流程缺口",
    "架构评审确认缓存分层与向量召回方案",
    "向量余弦相似度把记忆 A 与记忆 B 连成突触",
    "余弦相似度高的记忆之间会画一条突触连线",
    "用户偏好直接给出结论而不是铺垫",
    "深夜复盘发现情绪波动与发布事故高度相关",
    "同步平面按设备分目录写事件，天然免冲突",
    "光点沿突触线传导，抵达终点时神经元闪烁",
    "六引擎全部通过健康检查，冷启动约三点六秒",
    "丘脑路由把输入分成事实、偏好、情绪三类",
    "记忆分层每晚会把高频记忆晋升到核心层",
    "冲突检测发现两条关于发版时间的矛盾记录",
    "用户对深夜打扰比较敏感，提醒时间避开凌晨",
    "知识图谱里 Bob 负责发版流程",
    "关系抽取把负责人和维护者连成一条边",
    "备份脚本遇到文件锁会退化成流式复制",
    "事件日志是唯一真源，快照只是某一刻的状态",
    "按 HLC 排序可以保证跨设备事件顺序",
    "注入上下文按 L0 到 L4 收敛压缩",
    "召回时情绪记忆会获得更高的权重",
    "PAD 三元组让记忆带上情绪色彩",
    "Agent 基线情绪做低通滤波，记忆自身不改写",
    "离线写入不丢事件，恢复后自动补推",
    "多端接入用 token 决定命名空间",
)

PAGES = (
    (0, "01-nebula"),
    (1, "02-memory"),
    (3, "03-system"),
    (5, "04-model"),
    (6, "05-interface"),
    (7, "06-sync"),
    (8, "07-settings"),
)


def main() -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    for font_path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/simsun.ttc"):
        if Path(font_path).exists():
            QtGui.QFontDatabase.addApplicationFont(font_path)

    root = Path(tempfile.mkdtemp(prefix="brain_shots_"))
    config = BrainConfig(
        storage=StorageConfig(
            sqlite_path=root / "r.db", lancedb_path=root / "v.lance",
            kuzu_path=root / "g.kuzu", lmdb_path=root / "kv.lmdb", duckdb_path=root / "t.db",
        ),
        model=ModelConfig(dense_dim=8, use_cuda=False, llm_gguf_dir=root / "models"),
        sync=SyncConfig(device_id="dev-preview", local_repo_dir=root / "sync"),
    )
    config.activation.similarity_threshold = 0.22
    backend = DesktopBackend(config)
    try:
        for content in DEMO_MEMORIES:
            backend.save_memory(content)

        store = SettingsStore(config, keyring_backend=_NoKeyring(), qsettings=_MemorySettings())
        window = MainWindow(backend, store, config)
        window.resize(1440, 900)
        window.show()
        for _ in range(3):
            app.processEvents()

        out_dir = ROOT / "data" / "screenshots"
        out_dir.mkdir(parents=True, exist_ok=True)

        # 星云页：一次真实检索让光脉冲停在半空
        window.activate_query("发版冲突")
        for _ in range(6):
            window.nebula_page.view.animate()
        app.processEvents()

        for index, name in PAGES:
            window._nav.setCurrentRow(index)
            for _ in range(3):
                app.processEvents()
            if index == 6:  # 接口页顺手把服务跑起来，截图里能看到运行状态
                backend.start_http("127.0.0.1", 0)
                window.interface_page._refresh_ui()
                app.processEvents()
            pixmap = window.grab()
            path = out_dir / f"{name}.png"
            pixmap.save(str(path), "PNG")
            print(f"{path.name}  {pixmap.width()}x{pixmap.height()}")
        backend.stop_http()
        window.nebula_page.view.timer.stop()
        window._status_timer.stop()
        print(f"共 {len(PAGES)} 张 → {out_dir}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
