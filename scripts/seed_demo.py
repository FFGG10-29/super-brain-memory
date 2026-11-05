"""scripts/seed_demo.py —— 向真实库（data/）灌一批演示记忆。

    .venv\\Scripts\\python.exe scripts/seed_demo.py

注意：先关闭桌面窗口再运行（Kùzu 单进程写锁）；灌完重新 `python -m ui.desktop` 即可看到。
走完整写入流：丘脑路由 + SimHash 去重 + PAD + 六引擎 + 事件日志，重复运行自动去重。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import build_app  # noqa: E402

DEMO_MEMORIES = (
    # 发版主题（三条高相关 → 语义突触线）
    "周五发版出现严重冲突，全员加班到凌晨",
    "发版流程负责人更换为 Bob 并重新评审",
    "发版冲突复盘：凌晨加班暴露发布流程缺口",
    # 向量/突触主题（两条高相关）
    "向量余弦相似度把记忆 A 与记忆 B 连成突触",
    "余弦相似度高的记忆之间会画一条突触连线",
    # 架构与引擎
    "架构评审确认缓存分层与向量召回方案",
    "六引擎全部通过健康检查，冷启动约三点六秒",
    "丘脑路由把输入分成事实、偏好、情绪三类",
    "记忆分层每晚会把高频记忆晋升到核心层",
    "事件日志是唯一真源，快照只是某一刻的状态",
    "按 HLC 排序可以保证跨设备事件顺序",
    "注入上下文按 L0 到 L4 收敛压缩",
    "知识图谱里 Bob 负责发版流程",
    "关系抽取把负责人和维护者连成一条边",
    # 情绪与偏好（高唤醒 → 粉色节点）
    "用户偏好直接给出结论而不是铺垫",
    "用户对深夜打扰比较敏感，提醒时间避开凌晨",
    "深夜复盘发现情绪波动与发布事故高度相关",
    "PAD 三元组让记忆带上情绪色彩",
    "Agent 基线情绪做低通滤波，记忆自身不改写",
    "召回时情绪记忆会获得更高的权重",
    # 同步与运维
    "同步平面按设备分目录写事件，天然免冲突",
    "离线写入不丢事件，恢复后自动补推",
    "备份脚本遇到文件锁会退化成流式复制",
    "冲突检测发现两条关于发版时间的矛盾记录",
    "多端接入用 token 决定命名空间",
    "光点沿突触线传导，抵达终点时神经元闪烁",
)


def main() -> int:
    app = build_app()
    try:
        applied, skipped = 0, 0
        for content in DEMO_MEMORIES:
            result = app.hippocampus.remember(content, agent_id="seeder")
            mark = "+ " if result.applied else "= "
            print(f"{mark}{content}")
            applied += 1 if result.applied else 0
            skipped += 0 if result.applied else 1
        total = app.storage.relational.count("neuron", active_only=False)
        print(f"\n写入 {applied} 条，去重跳过 {skipped} 条；库内现有 {total} 条记忆")
        print("重新启动桌面前端即可看到：python -m ui.desktop")
        return 0
    finally:
        app.journal.flush()
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
