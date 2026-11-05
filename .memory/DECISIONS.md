# 实现级决策记录（ADR 简版）

> 只记录"为什么"，不记录"是什么"（是什么看代码）。

## ADR-001 SQLite 用标准库 `sqlite3`，不引入 SQLAlchemy ORM

- **决策**：`storage/relational_db.py` 直接用 `sqlite3`，手写 DDL。
- **理由**：六引擎是事件日志的投影，写入形态是"按 id 幂等 upsert + 少量查询"，
  ORM 带来的是映射开销与隐式事务，收益低；`sqlite3` 便于精确控制 `PRAGMA journal_mode=WAL`、
  `busy_timeout`、`synchronous` 与 thread-local 连接。
- **被否决**：SQLAlchemy Core（抽象层对 SQLite 特化 PRAGMA 覆盖不足）、SQLModel（绑定 Pydantic 版本）。

## ADR-002 存储层与认知层之间的契约是 `Mapping`，不是 Pydantic 实体

- **决策**：`core.interfaces` 中六个仓储接口收发 `Mapping[str, Any]` 记录；Pydantic 实体只活在 `models/` 与 `brain/`。
- **理由**：投影器重放事件时手里就是 JSON 字典，要求先构造实体再拆回来是纯损耗；
  且 sync 平面（NDJSON）天然是 dict。这样 storage 层不需要 import models，依赖方向保持单向。
- **代价**：类型检查弱一点，由 `models/` 的校验在写入入口（丘脑）兜住。

## ADR-003 一个 Kuzu `Database`，两套逻辑视图（graph_db / hypergraph_db 共享）

- **决策**：`graph_db` 持有 `kuzu.Database`；`hypergraph_db` 通过注入同一实例获得 `Connection`。
- **理由**：实测同进程对同一 kuzu 文件开第二个 `Database` 会争锁；架构上超图本就是"逻辑独立、物理复用"。
- **How to apply**：`HypergraphDB(graph=graph_db)`，禁止自行 `kuzu.Database(path)`。

## ADR-004 DuckDB 单连接 + 每线程 cursor

- **决策**：`time_series_db` 只建一个 `duckdb.connect()`，线程安全靠 `conn.cursor()`。
- **理由**：DuckDB 是单写模型，多连接写同一文件直接冲突；`cursor()` 才是官方线程隔离手段，
  与"每引擎 thread-local 连接"的架构纪律在效果上等价（写路径仍串行）。

## ADR-005 降级必须"可测"，不靠真的装不上模型

- **决策**：`engine/*` 把 provider 解析拆成纯函数（`resolve_onnx_providers(...)`），
  接受"可用 provider 列表"作为入参；真实探测只是默认参数。
- **理由**：验收要求"断 CUDA 落 CPU"必须能稳定复现，不能依赖本机 GPU 状态。

## ADR-006 稀疏向量暂以 JSON 文本列存储

- **决策**：BGE-M3 的 lexical weights 以 `JSON` 字符串落入 LanceDB 的 `sparse` 列，召回时在候选集上线性打分。
- **理由**：LanceDB 0.41 无原生稀疏向量索引；在 k×4 超采样后的候选集（数百条）上线性打分成本可忽略，
  且保持"预过滤先于打分"的结构。后续若候选集变大再换倒排。

## ADR-007 记忆自身的 PAD 不平滑，平滑只作用于 Agent 情绪基线

- **决策**：`Neuron.pad` 存"事件原始情绪坐标"（`Amygdala.refine()` 直接透传）；`Amygdala.state(agent).pad` 才是低通平滑后的基线。
- **理由**：若对存入记忆的 PAD 做平滑，高强度单次事件会被历史均值稀释，等于在存储端就抹掉了"闪光灯记忆"；
  而情绪基线本来就是慢变量，需要平滑的只有它。低通积分只应用于 `affinity`（好感度）。
- **How to apply**：需要"当前用户情绪"读 `amygdala.state()`；需要"这条记忆的情绪色彩"读 `neuron.pad`。
  冷静期折扣只乘在召回分数上，绝不回写已存记录的 PAD。

## ADR-008 桌面前端用 PySide6，光脉冲由真实相似度驱动

- **决策**：`ui/desktop/` 用 PySide6 QGraphicsView；节点来自真实记忆库，语义边来自
  `brain/activation.py` 的余弦相似度，光点速度/亮度 = 相似度；查询中心→N 条命中齐射由
  `prefrontal.recall()` 结果触发。UI 只经 `DesktopBackend` 取数据，不碰 storage 实现。
- **理由**：用户要的是"突触激活的视觉具象化"——光点必须是检索事件，不能是随机动画；
  用随机 demo 数据会掩盖检索链路是否真的接通。无 ONNX 模型时降级为字符 2-gram TF 向量
  （确定性、跨设备一致），并在 `ActivationGraph.degraded` 明示，绝不伪造相似度数值。
- **How to apply**：新增可视化元素时，参数进 `config.UiConfig`/`ActivationConfig`；
  视图不得改记忆、不得直接读库；场景坐标用 `scene.setSceneRect(0,0,1000,800)` 固定，
  否则节点包围盒变化会让离屏渲染整体缩放。
- **PAT 安全**：PAT 只进系统钥匙串（keyring）或 `GITHUB_PAT` 环境变量；
  `SYNC_REPO_URL` 进用户级 QSettings；两者都不写项目 `.env`、不写事件日志、不在 UI 回显明文。
