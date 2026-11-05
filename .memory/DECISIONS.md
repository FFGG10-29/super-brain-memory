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

> v2.2 已扩到十一页（新增「模型配置」「事件时间轴」），外壳组织方式不变，详见 §十四 / ADR-013/014。

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

## ADR-011 用户控制（纠正/编辑/封存/遗忘）复用 7 类事件，绝不原地改 payload

- **决策**：`brain/memory_editor.py` 只发三种既有事件：纠正/编辑 = 新 `ingest` + 旧 `supersede`
  （payload 带 `before/after/actor/reason`）；遗忘 = `tombstone`（`action` 区分 forget / archive 语义）。
  写入顺序固定
  `journal.append(fsync) → 主库 → 派生库/outbox → dispatch`。
- **理由**：① 事件契约不扩张，跨设备重放与 `EventProjector` 无需改动即天然可用；
  ② 主库 upsert 与 `vector.upsert` 都是**整行替换**，原地改 payload 会直接抹掉审计原值，
  而「原来说的是什么」是可信记忆的必要条件；③ 遗忘做成 `tombstone`（从推理路径移除、审计保留）
  而不是物理删除——物理删了之后，任何一台落后设备重放 journal 都会把它**复活**。
- **被否决**：新增 `correct` / `forget` 事件类型（要改契约 + 双端全量重放兼容）；
  物理删除（跨设备重放语义不成立）。
- **How to apply**：新增可编辑字段 → 加进 `_EDITABLE` 白名单，payload 快照里同步带上，
  否则审计看不到这次变化；合规硬清除另建 purge 流，需同步清向量/图谱/时序/KV，不能只删主库行。

## ADR-012 稀疏通道与全文退化路径一律走「派生索引 + 可重建」

- **决策**：`storage/vector_db.py::_SparseIndex` 用同目录 SQLite 存两张倒排表：
  `postings(term,id,weight,...)` 与 `text_postings(term,id)`（正文 2-gram），外加 `docs` 轻量表。
  `search_sparse` / `search_text` 先走倒排，索引不可用或行数对不上就整表重建，再不行退回原全表扫描。
  `brain/activation.py` 的构图缓存同理：gzip JSON 落盘，靠「模型/参数/权重指纹」签名判失效。
- **理由**：LanceDB 0.41 没有原生稀疏索引，原实现每次检索 `to_pylist()` 全表 + 逐行 `json.loads`，
  10 万条时这是主要瓶颈；而缓存/索引一旦被当作真源，就会出现「库对了缓存错了」的静默错误。
  因此两者都定位为**派生数据**：删掉、损坏、签名变化都能重建，且必须与全扫结果一致。
- **代价**：多一个 SQLite 文件与一次重建成本；正文更新要同步更新倒排（`upsert`/`delete` 已覆盖）。
- **How to apply**：改打分/改分词必须同时改倒排与全扫两条路径，并在测试里断言「索引结果 == 全扫结果」。

## ADR-013 星云布局按「分层环 + 类型扇区 + 时间角度」语义化，仍是纯确定性函数

- **决策**：`ui/desktop/neurons.py::_semantic_position`：半径由 tier 决定（core 内圈 → archive 外圈），
  扇区由 `memory_type` 决定（fact/profile/emotion/scenario/code），扇区内角度按 uuid7 时间戳
  以 6 小时为粒度映射；同 id 同元数据 → 同坐标。
- **理由**：原稳定哈希布局虽然可复现，但同类记忆随机散开，看不出「核心/缓冲/归档」与
  「事实/情绪/画像」的结构；用户要的是**认知分区**而不是好看的点云。语义化坐标不引入随机数，
  所以跨设备重放与离屏截图仍逐像素可复现（测试与截图脚本依赖这个性质）。
- **How to apply**：新增 memory_type 要同步 `_TYPE_SECTOR` 与数据页 `_SECTOR_COLORS`；
  调整布局只改这一处纯函数，不要往节点对象里塞运行期状态。

## ADR-014 重活出主线程 + 冻结态资源路径与 CUDA DLL 预登记

- **决策**：① `ui/desktop/async_backend.py` 用 `maxThreadCount=1` 的 QThreadPool 串行执行后端调用，
  结果经 `QueuedConnection` 回主线程刷新控件；后台忙时 `refresh_status()` 直接跳过本轮。
  ② `core/runtime_paths.py` 区分 dev（项目根）/ frozen（`_MEIPASS` 与 exe 目录、用户数据目录）。
  ③ `engine/cuda_runtime.py::prepare_cuda_dll_path()` 在 `main.py` **顶部**（任何 `llama_cpp` /
  `onnxruntime` import 之前）登记 `cuda_bin/`，并做「驱动版本是否够 + DLL 是否齐」的分级判定。
- **理由**：① 模型推理与六路召回是秒级操作，放主线程会让星云动画卡死、状态栏排队等锁；
  单线程池保证同一时刻只有一个任务碰六引擎（Kùzu / DuckDB 都不是多写安全的）。
  ② 打包后写安装目录会失败（Program Files 无权限）、写 `_MEIPASS` 会在退出时丢数据。
  ③ Windows 上 cuDNN 子库靠 DLL 搜索路径解析，**import 之后再改 PATH 无效**，
  结果是「装了 CUDA 版 wheel 却静默跑 CPU」——这类问题只能靠预登记 + `--probe-models` 主动验证。
- **How to apply**：新增耗时后端方法一律通过 `tasks.submit()` 调用，不要在页面里直接同步调；
  新资源文件放 `resource_root()`，新可写数据放 `data_dir()`；`self_check` 不能用于判断 GPU 是否生效。

## ADR-015 超级大脑是「所有 Agent 共用的外置大脑」，agent_id 只审计不隔离

- **决策**：`MCPToolkit` 的默认命名空间取 `config.namespace_policy`（单一共享库），
  召回/画像查询**不按 namespace 过滤**（`namespaces=()`）；`agent_id` 只写审计与限流。
  只有客户端**显式**传 `namespace` 时才做权限校验。
- **理由**：产品定位是「外置记忆中枢」——CodeBuddy / DSH / WorkBuddy 必须看到同一份记忆。
  按 agent 隔离会让每个 Agent 只记得自己的事，等于把共享大脑退化成各家的私有笔记。
- **How to apply**：新增工具默认走共享库；要隔离的场景用显式 `namespace` 参数，
  别把 `agent.scopes[0]` 当默认值（历史实现踩过这个坑）。

## ADR-016 多 Agent 并发只在「入口」排队，存储仍是单进程独占

- **决策**：桌面内核独占六引擎（Kùzu 一个库只允许一个进程）；HTTP 适配器的所有工具调用进
  一个 `queue.Queue`，由**唯一工作线程** FIFO 执行，`queue_depth` 暴露给 UI；
  各 Agent 的 stdio 进程通过 `%APPDATA%/super-brain/mcp_bridge.json`（地址 + 本机桥接密钥）
  转发 `/mcp/call`，不自己开库。
- **理由**：并发直写六引擎既不安全（ONNX 会话/写锁）也不符合语义——准入、去重、冲突消解
  必须由中枢按顺序裁决。桥接密钥与 registry 令牌解耦，避免"未配 GW_MCP_TOKEN_SECRET 时
  重启即 401"。
- **How to apply**：新增入口一律走 `HTTPAdapter._call_tool()`；不要让 stdio 进程直接 `build_app()`，
  除非确认桌面没开（那是独占模式，且只能有一个 Agent）。

## ADR-017 桌面端长期驻留：托盘 + 单实例 + 固定启动器

- **决策**：① `setQuitOnLastWindowClosed(False)`，叉窗口只 `hide()`，托盘菜单「完全退出」才真退；
  ② 单实例用 `QLocalServer`，第二次启动把已有窗口唤到前台后自身退出；
  ③ 开机自启写 `HKCU\...\Run`，命令带 `--tray`；
  ④ MCP 客户端配置里只出现 `cmd.exe /c %LOCALAPPDATA%\SuperBrain\bin\super-brain-mcp.cmd`，
  启动器每次启动按当前安装位置重写。
- **理由**：用户要「开机启动一次、常驻后台、随时进前端」；而把安装绝对路径写进 Agent 的
  mcp.json 会在换盘（B:→D:）后失效，且用户不会想手动改十份配置。
- **How to apply**：任何"应用搬家"场景只需启动一次桌面端；托盘图标缺失时（无系统托盘）
  自动退回普通关窗行为；`--tray` 与正常启动共用同一套初始化，只是不 `show()`。

## ADR-018 对外只暴露「标准 MCP」，令牌持久化且与运行状态解耦

- **决策**：① 接口页只给两份可直接粘贴的 `mcpServers` JSON：`streamable-http`
  （`POST /mcp`，`Authorization: Bearer <桥接令牌>`）与 `stdio`（本机启动器），不再输出散文说明；
  ② 桥接令牌落 `%APPDATA%/super-brain/mcp_token.txt`，服务启动优先复用，**重启不变**；
  ③ `/mcp` 用最小但合规的 JSON-RPC（initialize / tools.list / tools.call / ping），
  不实现服务端主动 SSE；④ 认证同时接受 `X-Super-Brain-Bridge` 与 `Authorization: Bearer`。
- **理由**：用户真正要的是「复制一段 JSON 粘进 Agent 就能用」。此前每次启动 HTTP 都重新随机生成
  token、`mcp_bridge.json` 又会随停服清空，导致刚复制的配置过一会儿就 401——这不是安全问题，
  是把内部实现细节（会话级令牌）泄漏到了用户配置里。标准 MCP 端点也让非 Python 客户端
  （Node / Go / 桌面 Agent）不必理解我们的私有 `/mcp/call`。
- **How to apply**：新增工具只改 `mcp_stdio.MCP_TOOL_DEFS` 一处，stdio 与 HTTP 自动同步；
  任何需要认证的入口都要同时接受 Bearer 与桥接头，否则会被只带 Authorization 的客户端 401。

