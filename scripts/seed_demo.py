"""scripts/seed_demo.py —— 向真实库（data/）灌一批演示记忆。

    .venv\\Scripts\\python.exe scripts/seed_demo.py

注意：先关闭后端进程再运行（Kùzu 单进程写锁）；灌完重启后端、用 Flutter 前端即可看到。
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
    # 生活与偏好（拉开记忆类型与 PAD 分布，星云里会有更多情绪节点）
    "周末喜欢去城郊的旧书店，一待就是整个下午",
    "手冲咖啡偏好浅烘豆，水温稳定在九十二度",
    "跑步习惯放在清晨六点，配速大约六分半",
    "养了一只叫豆包的橘猫，晚上总趴在键盘旁边",
    "听爵士乐写代码的效率明显高于听流行乐",
    "下雨天容易走神，适合整理笔记而不是写代码",
    "每年十一月会重读一遍架构设计的老笔记",
    "旅行时更愿意逛本地菜市场而不是热门景点",
    "家里那盆琴叶榕需要每周浇一次水",
    "和家人的通话固定在周日晚上八点",
    "对甜食不太感冒，却很喜欢咸口的小点心",
    "第一次独立完成发版那天特别有成就感",
    "连续熬夜之后情绪明显低落，需要大半天恢复",
    "被临时打断后很难立刻回到深度思考状态",
    "更信任能复现的实验数据，不太喜欢口头结论",
    "新工具先小范围试用一周，再决定要不要全面切换",
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
        print("重启后端后用 Flutter 前端即可看到记忆星云")
        return 0
    finally:
        app.journal.flush()
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
