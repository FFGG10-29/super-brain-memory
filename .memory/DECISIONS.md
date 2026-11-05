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
  视图不得改记忆、不得直接读库；场景坐标用 `sceneRect(0,0,1000,800)` 固定，
  否则节点包围盒变化会让离屏渲染整体缩放。
- **命名坑**：`QGraphicsView.scene` / `QGraphicsItem.data` 是 Qt 保留方法，直接当属性名会被覆盖；
  本项目用 `NebulaView.view_scene` 与 `NeuronNode.node`。
- **PAT 安全**：PAT 只进系统钥匙串（keyring）或 `GITHUB_PAT` 环境变量；
  `SYNC_REPO_URL` 进用户级 QSettings；两者都不写项目 `.env`、不写事件日志、不在 UI 回显明文。

## ADR-009 桌面外壳按 LivingMemory 组织成九页控制台，端口服务真实启停

- **决策**：外壳 = 左侧九页导航（星云 / 记忆管理 / 召回测试 / 系统概览 / 展示数据 / 接口与端口 /
  同步与备份 / 设置 / 关于）+ 顶栏（空间选择、返回全景、全局搜索、导出、管理数据、全屏）+
  满屏星云 + 右下迷你地图 + 底部统计条。"接口与端口"页直接调 `HTTPAdapter.make_server()`
  在后台线程起真实服务，端口被占用返回结构化错误，停止走 `shutdown()+server_close()`。
- **理由**：参考图（LivingMemory）的功能密度要求"一屏可视化 + 侧栏管功能"；把端口/同步/调度
  塞进同一个星云窗口会让高频操作被埋。端口是最常改的运维参数，必须能改完立刻验证
  （浏览器打开 `/health` 或 curl），而不是改 `.env` 后重启进程。
- **How to apply**：新增功能 = 新增一页 + 侧栏项 + 状态栏字段；页面只 import `DesktopBackend`，
  不 import `gateway.*` / `sync.*` 实现；端口/帧率/阈值等偏好统一走
  `SettingsStore.save_prefs()`（用户级 QSettings + 即时回写 config）；端口测试用 `port=0`
  让内核分配空闲端口，避免 CI 端口冲突（`make_server` 已修正 `port or default` 吞 0 的问题）。
- **UI 一致性**：所有颜色/字号走 `theme.stylesheet()`，页面禁止内联大段样式；
  动画参数走 `config.UiConfig`，设置页改完即时生效（`appearance_changed` 信号）。

## ADR-010 配置中心：差异补丁 + 预设 + 自动表单 + 空引擎占位

- **决策**：打包后所有参数前端可调、免重编译。
  1) 用户配置只存"与出厂默认的深合并差异补丁"（`%APPDATA%/super-brain/config.json`），
     软件升级新增参数自动落默认值；`DesktopBackend.start()` 前应用补丁；
  2) 三套预设（均衡默认 / 性能全开 / 低配省电）= 一组补丁；
  3) 设置页用 `ConfigEditor` 按 pydantic 注解自动渲染（bool→勾选、Literal→下拉、
     dict[str,bool]→逐键勾选、Path→文本框），新增 config 字段零 UI 代码；
  4) 存储引擎开关：relational 恒开；关闭=空实现占位（写 no-op、读空、health 标"已关闭"），
     事件日志是唯一真源，重开后可重放重建；
  5) 模型 `local/cloud/auto` 三态：云端走 OpenAI 兼容 HTTP（urllib），失败抛
     EngineUnavailable 走既有降级，未配齐自动退本地，保证"配一半"也能开机；
  6) 启动期参数（引擎开关/模型模式/维度）保存后提示「应用并重启内核」，
     `DesktopBackend.restart()` 关旧引擎→重放补丁→重建，数据不丢。
- **理由**：多端安装意味着设备能力差异大（有无 GPU、磁盘大小、是否常离线），
  编译期常量无法适配；差异补丁比全量导出安全（升级不丢用户调参）；
  空实现占位而非跳过装配，保证依赖图（hippocampus→六库）结构稳定，测试与生产同构。
- **How to apply**：新增可调参数 = 在 `config.py` 对应子模型加字段 + `FIELD_LABELS` 加中文名，
  UI 自动出现；需要重启生效的参数加进 `save_settings_patch` 的 restart_needed 判定；
  新密钥一律走钥匙串（account 约定见 `SettingsStore`），配置文件里只允许非敏感值。
