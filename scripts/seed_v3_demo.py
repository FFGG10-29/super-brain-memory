"""scripts/seed_v3_demo.py —— 通过 /v1/events 受理一批主题分组记忆，供 3D 星云展示。

    .venv\\Scripts\\python.exe scripts/seed_v3_demo.py

主题内文本高度重合 → char-2gram 余弦形成突触边 → 连通分量聚成星系团。
写入是异步的（202 accepted），脚本等待 inbox 追平后打印图统计。
"""
from __future__ import annotations

import json
import time
import urllib.request

BASE = "http://127.0.0.1:8768"

GROUPS = {
    "发版事故与复盘": (
        "周五发版出现严重冲突，全员加班到凌晨三点",
        "发版冲突复盘：凌晨加班暴露发布流程缺口",
        "发版流程负责人更换并重新评审发布清单",
        "发版窗口调整为周三下午并增加灰度阶段",
        "发版前的自动化回归用例补齐了二十条",
    ),
    "缓存与向量召回": (
        "缓存服务迁移到新机房并完成压测",
        "缓存服务新机房的限流策略已配置",
        "缓存服务迁移的回滚预案完成演练",
        "向量召回引入余弦相似度重排序提升命中",
        "向量库维度从 768 调整为 1024 后重建索引",
        "缓存与向量召回联动显著降低首字延迟",
    ),
    "多端同步与事件日志": (
        "事件日志是唯一真源，快照只是某一刻的状态",
        "多端同步按设备分目录写事件，天然免冲突",
        "按 HLC 排序可以保证跨设备事件因果顺序",
        "离线写入不丢事件，恢复后自动补推",
        "同步平面写入防抖把连续写入合并成一次推送",
    ),
    "情绪与冷静期": (
        "深夜复盘发现情绪波动与发布事故高度相关",
        "PAD 三元组让每条记忆带上情绪色彩",
        "冷静期内相关记忆召回打折避免上头决策",
        "用户对深夜打扰比较敏感，提醒时间避开凌晨",
    ),
    "偏好与画像": (
        "用户偏好直接给出结论而不是长篇铺垫",
        "用户习惯先看数据再决定是否调整架构",
        "用户要求所有参数都能在界面上直接调整",
        "用户希望星云像星系一样一坨一坨有层次",
    ),
}


def post(content: str, agent_id: str) -> dict:
    body = json.dumps({"content": content, "agent_id": agent_id}).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}/v1/events", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 本地回环
        return json.loads(response.read().decode("utf-8"))


def get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    total = 0
    for topic, texts in GROUPS.items():
        for text in texts:
            result = post(text, agent_id=f"demo-{topic}")
            total += 1 if result.get("accepted") else 0
    print(f"已受理 {total} 条主题分组记忆")
    deadline = time.time() + 30
    while time.time() < deadline:
        health = get("/v1/health")
        done = health["inbox"].get("done", 0)
        pending = health["inbox"].get("pending", 0)
        if pending == 0 and done >= total:
            break
        time.sleep(0.3)
    graph = get("/v1/graph")
    print(f"投影：{len(graph['nodes'])} 节点 / {len(graph['edges'])} 突触边")
    print("打开 Flutter 客户端即可看到按主题聚成的多个星系盘")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
