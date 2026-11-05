# LIFECYCLE_TRACE —— 一条记忆从 MCP 进来到进记忆库的完整链路（案例讲解）

> 案例：WorkBuddy 在对话里听到你说「记忆系统的项目排期推迟到 6 月 1 日」，
> 调用 `save_memory(content="记忆系统的项目排期推迟到 6 月 1 日", agent_id="workbuddy")`。

## 0. 进程启动时（一次性）

MCP 客户端拉起 `python -m gateway.mcp_stdio` → 组合根 `build_app()`：
六引擎连接/建表 → 三模型懒加载（不预热，首条才加载）→ HLC 时钟 → 丘脑/杏仁核/海马体/
前额叶/新皮层/可塑性装配 → 语义合并器挂进写入流。冷启动预算 30s（实测 ~3.7s）。

## 1. MCP 工具路由（gateway/mcp_stdio.py）

客户端 `initialize` → `tools/list`（6 个工具自描述）→ `tools/call save_memory`。
`agent_id="workbuddy"` 随调用传入，此后这条记忆永远知道自己来自谁。

## 2. RouterExtractor 抽取（brain/router_extractor.py）

LLM（你配的本地 Qwen 0.6B 或云端模型）输出严格 JSON：

```json
{"memory_type": "fact",
 "entities": [{"name": "记忆系统", "type": "project"}, {"name": "项目排期", "type": "concept"}],
 "relations": [{"subject": "项目排期", "predicate": "推迟到", "object": "6 月 1 日"}],
 "pad": {"p": -0.1, "a": 0.3, "d": 0.1}, "confidence": 0.85}
```

LLM 超时/输出不合法 → 正则规则兜底（日期/冒号/「把…改成…」句式）。
**对比**：Mem0 用 GPT-4 级做同一步，质量更高但必须联网付费；本系统可本地可云端（模型配置页切换）。

## 3. 杏仁核精算（brain/amygdala.py）

- 记忆自身 PAD：照事件原样存（ADR-007：闪光灯记忆不被历史抹平）；
- Agent 情绪基线：好感度做低通积分（affinity_alpha=0.15），整体情绪轻微偏移。

## 4. 丘脑门控（brain/thalamus.py）

- 长度 ≥4 字 ✓；
- SimHash 指纹与最近 200 条比对，汉明距离 ≤3 判重复 → 拒收（这条是新信息，通过）；
- 显著性 = 信息量(0.3) + 情绪(0.5) + 置信度(0.2) ≥ 0.30 → **产生跨设备同步事件**；
- 限流检查（workbuddy 每分钟 ≤60 条）。

## 5. 语义合并裁决（brain/memory_consolidator.py，Mem0 式）

向量 ANN + FTS 滑窗召回最相似旧记忆 → 找到「项目排期定在 5 月 20 日」→
LLM 裁决：`{"action": "update", "content": "项目排期从 5 月 20 日推迟到 6 月 1 日",
"supersede_ids": ["旧id"], "reason": "排期变更"}`。
LLM 不可用 → 兜底 add（绝不丢信息）。**对比**：这是 Mem0 的招牌能力，Zep 没有；
update/merge 会把旧记忆**封存而非删除**（双时态打点），Zep 的 invalidation 同级，Mem0 是改写。

## 6. 落库（唯一事实源优先）

SQLite 主库 upsert（正文/类型/分层/PAD/双时态/来源=workbuddy）→ outbox 事件队列入列。

## 7. 六引擎幂等投影（outbox 分发）

- **LanceDB**：BGE-M3 dense+sparse 向量（语义联想通道）；
- **Kùzu**：实体节点 + `项目排期-推迟到->6 月 1 日` 关系边 + 超边（记录涉及哪些实体）；
- **DuckDB**：PAD 轨迹 + 可塑性衰减快照（append-only）；
- **超图**：多主体事件的高阶关联。
任一投影失败不回滚事实，失败项留在 outbox 重试；引擎被关则该通道 no-op（事件可重放重建）。

## 8. 冲突消解（ hippocampus._resolve_conflicts）

同 (subject, predicate) 旧三元组置信度比较：新 0.85 ≥ 旧 → 旧记录封存 + 图谱 supersede 演进边。
永不物理删除——「我上周还以为排期是 5 月 20 日」可追溯。

## 9. 事件日志（sync/journal.py，fsync 先于回执）

NDJSON ingest 事件（含完整 neuron + PAD + 抽取）fsync 落盘 → **这是跨设备的唯一真源**。
GitSyncWorker 防抖 2s 后 pull→replay→push 到 GitHub 私库 → 显卡机/其它设备 pull 后
幂等重放（mark_event_applied 去重），PAD 与记忆跨设备流动。

## 10. 可观测

structlog 记录 ingest（agent/通道/结果）→ 「系统概览」计数 +1 →
「记忆管理」可查详情（双时态/PAD/可塑性）→ 星云视图上这条记忆作为新节点出现，
与「项目排期」相关旧记忆之间因相似度画出突触线。

## 主流方案同环节对照

| 环节 | Mem0 | Letta | Zep/Graphiti | 本系统 |
|---|---|---|---|---|
| 抽取分类 | 强模型 | agent 自写 | 强模型 | 本地/云端可选 |
| 合并裁决 | ✅ ADD/UPDATE/DELETE | 🟡 self-edit | 🟡 invalidation | ✅ add/update/merge/skip |
| 冲突处理 | 合并改写 | 分区覆盖 | invalid_at | 封存+演进边（不删除） |
| 情绪 | ❌ | ❌ | ❌ | ✅ PAD 全链路 |
| 遗忘 | ❌ | 🟡 | 🟡 | ✅ FSRS 量化 |
| 跨设备 | 云托管 | ❌ | 服务端 | ✅ 事件溯源+Git 总线 |

## 附 1. 用户纠正一条已存在记忆（v2.2）

场景：用户在「记忆管理」里点开「项目排期推迟到 6 月 1 日」，发现日期写错，改成「6 月 8 日」并顺手提到 core 层。

1. **入口**：`MemoryInspector` 编辑区 → `DesktopBackend.correct_memory(id, {"content": ..., "tier": "core"}, reason=...)`
   → `brain/memory_editor.py`。
2. **journal 先落盘**：`ingest`（新 neuron，`extraction.source="user_correction"`）+ `supersede`
   （payload `{old_id, new_id, before: 完整旧记录, after: 完整新记录, changed: ["content","tier"],
   actor: "user", reason, ts_ms}`），fsync 后才返回。
3. **主库**：新行 upsert；旧行 `is_active=0`、`tx_to/valid_to` 打点，**content 原值一字不改**。
4. **派生库**：向量库 insert 新行 + `deactivate` 旧行；图谱补 `SUPERSEDES` 演进边；outbox 入列分发。
5. **审计**：`memory_audit(new_id)` 返回 `ingest` + `supersede`，`changed` 里直接能看到 content 前后值——
   这就是检查器「历史」面板的数据源，无需另建审计表。
6. **跨设备**：另一台机器 pull 到这两个事件后重放：旧行被封存、新行出现、`SUPERSEDES` 边一致。
   因为没新增事件类型，`EventProjector` 无需任何改动。

## 附 2. 用户遗忘一条记忆（tombstone 语义）

1. `DesktopBackend.forget_memory(id, reason=...)` → `tombstone` 事件
   （payload 带 `neuron_id / before / actor / reason / action`；`action="forget"` 为用户遗忘，
   `action="archive"` 表示封存语义，供审计区分）。
2. `deactivate` 主库行 + `vector.delete(id)`：**推理路径上彻底消失**——召回、列表、星云都不再出现。
3. 主库封存行与 journal 事件保留：审计可回答「这条曾经存在、被谁在什么时候遗忘」。
4. 重放语义：任何落后设备重放 tombstone 后同样隐藏该条，不会复活。
   → 若做物理删除，落后设备重放 `ingest` 就会把已删记忆重新造出来（这就是不物理删的原因）。
5. 合规硬清除（GDPR 式「彻底抹掉」）不在本链路：需另建 purge 流，同步清向量/图谱/时序/KV，
   且必须带 tombstone 前置，否则重放链路会把数据带回。

## 附 3. Core Memory 编辑（policy 事件）

1. 数据页「Core Memory」四个块（human / persona / preferences / constraints）→
   `DesktopBackend.save_core_memory_blocks({...})` 一次保存全部块。
2. 写 KV `core_memory/blocks`，并只发**一条** `policy` 事件
   （`payload={"bucket": "core_memory", "key": "blocks", "blocks": {...}, "reason": "edit_core_memory"}`）——
   逐块发事件会让审计里全是噪音。
3. `sync/projections.py` 对 `bucket == "core_memory"` 走 `kv.put_json` 分支，重放到同一 bucket，
   所以另一台设备重放后核心记忆块一致。
4. 抽取/召回的注入侧把四块当稳定背景信息使用；它们不是记忆条目，不参与 FSRS 衰减与分层。

# PRODUCTION_READINESS —— 生产就绪与构建评估（单用户场景）

## 已达标（可视为生产级）

- 功能完整性：七阶段验收 + PAD 全链路 + 合并/聚类 + 用户控制闭环 + 123 用例全绿，`--self-check` 退出码 0；
- 数据安全：fsync 事件日志、幂等投影/重放、快照、封存/遗忘不物理删除、备份脚本（含文件锁退化）；
- 故障降级：任一引擎关闭、模型缺失、云端超时、索引/缓存损坏都有确定行为，不崩不挂；
- 多端：HLC 因果序、设备分目录、冲突可追溯、用户纠正与遗忘可重放；
- 可观测：结构化日志 + 健康检查 + 控制台看板 + 事件时间轴 + 记忆检查器（来源/双时态/审计）；
- 接入：MCP stdio + HTTP OpenAI 兼容（token/命名空间隔离）。

## 上真机前建议补（半天内可完成）

1. 设置 `GW_MCP_TOKEN_SECRET` 环境变量（否则 HTTP 令牌重启失效）；
2. 跑一次「快照 → 删库 → 快照+尾部重放恢复」演练（脚本已有，未实弹）；
3. 显卡机上验证 CUDA：`use_cuda=true` + `llm_gpu_layers=-1`（模型配置页/预设「独显」），
   然后**必须**跑 `main.py --probe-models --json` 看 `verdict`（`--self-check` 只构造对象，证明不了加载）；
4. 模型权重落位：BGE-M3（含 `sparse_head.onnx`，由 `scripts/export_sparse_head.py` 从 `sparse_linear.pt` 导出）
   / bge-reranker ONNX 与 Qwen GGUF 放 `data/models/`；
5. 便携发行演练：`scripts/build_portable.py` → PyInstaller onedir → `scripts/verify_portable.py` 要求换目录后仍 `GPU_OK`。

## 构建与分发建议

- **方案 A（推荐）**：GitHub 同步 → 显卡机 `git clone` → `python -m venv` +
  `pip install -r requirements.txt` → 放权重 → `--self-check` → `python -m ui.desktop`。
  十分钟内可跑，权重不进 Git。
- **方案 B**：PyInstaller 单 exe。PySide6/onnxruntime/llama-cpp/kuzu/lmdb 都有原生二进制，
  hidden imports 坑多，预计 1-2 天调通；建议实测满意后再做。
- 抽取质量、单机规模、生态早期、评估盲区：按你的定义不阻塞生产。
