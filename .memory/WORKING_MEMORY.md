# 工作记忆 · 当前状态

> 最后更新：**2026-11-09** ｜ 当前阶段：**Flutter 前端定稿并已出安装包**（MSVC 已装，`flutter build windows --release` 通过；7 组主导航 + 组内子标签；3D 星云改为「星团云气 + 分类着色 + 细星点」；首屏 0 节点与节点糊成光球两个致命问题已定位修复；MCP 53 工具 / 6 resources 可从任意 Agent 直接接入；`flutter analyze` 0 issues、`pytest 141 passed`）

## 1. 进度总览

| 阶段 | 状态 | 备注 |
|---|---|---|
| 骨架 | ✅ | 目录树 + 全模块契约 docstring |
| 0 地基 | ✅ | 依赖锁定 + `scripts/verify_baseline.py`；冷启动 1.1~3.7s |
| 1 写入流 | ✅ | 三实体 + 抽取 + 去重 + outbox + 冲突消解 |
| 2 召回流 | ✅ | 六路 RRF + 精排 + 配额 + 注入；golden-set 与 P95 用例通过 |
| 3 生命循环 | ✅ | 分层迁移、金字塔固化、昼夜调度、遗忘清扫、晨报 |
| 4 网关多端 | ✅ | registry + MCP 工具面 + HTTP/OpenAI 适配（真实端口冒烟通过） |
| 5 同步平面 | ✅ | projections/git_worker/snapshot；双机收敛、断网零丢失、幂等、冷启动重建全测过 |
| 6 可视与打磨 | ✅ | 六面板数据源 + Dashboard + eval 基线 + 结构化日志 + 备份脚本 |
| PAD 全链路 | ✅ 8/8 | `tests/test_pad_chain.py` |
| PySide6 桌面控制台 | ✅ | `ui/desktop/`：十一页导航（星云/记忆/召回/系统/数据/模型配置/接口端口/同步/设置/关于/**事件时间轴**） |
| 配置中心（免重编译） | ✅ | 预设（均衡/核显/独显）+ 全量参数自动表单（高级参数折叠）+ 存储引擎开关 + 模型双通道（embed/llm 独立供应商）+ 内核热重启 |
| 语义合并 + 聚类摘要 | ✅ | `brain/memory_consolidator.py`：写入裁决 add/update/merge/skip；夜间聚簇 LLM 主题摘要；`tests/test_semantic_merge.py` |
| MCP stdio 接入 | ✅ | `gateway/mcp_stdio.py`：save/recall/timeline/profile/conflict/health 六工具，外部 Agent（WorkBuddy/CodeBuddy/DSH）可挂载 |
| 记忆批量导入 | ✅ | txt/md/json/ndjson/csv → 完整写入流；导出 ndjson 双向打通 |
| 评估报告 | ✅ | `.memory/EVALUATION.md`（对标 Mem0/Letta/Zep/腾讯，两大短板已补齐）+ `.memory/LIFECYCLE_TRACE.md`（MCP 记忆全链路案例） |
| 用户控制闭环 | ✅ | `brain/memory_editor.py`：纠正/编辑/封存/遗忘 → `ingest`+`supersede` / `tombstone` 事件；原值就地可查；`tests/test_user_control.py` |
| 事件时间轴页 | ✅ | `ui/desktop/pages/timeline_page.py`：journal HLC 全序 + 30min 会话分组 + EventType 筛选 + 3s 轮询 + 双击直达检查器 |
| 敏感遮蔽 + 命名空间预过滤 | ✅ | 检查器「在当前上下文显示」就地揭示；`backend.set_sensitive()` → KV 标记；记忆页/召回页命名空间下拉，过滤下推到 `prefrontal.recall(namespaces=...)` |
| 星云语义布局 + 认知分区 | ✅ | `ui/desktop/neurons.py::_semantic_position`：环=分层 / 扇区=类型 / 角度=形成时间（仍纯确定性）；数据页「认知分区」五类柱图 |
| Core Memory 可编辑 | ✅ | 数据页四块（human/persona/preferences/constraints）→ KV `core_memory/blocks` + 单条 `policy` 事件，重放可复现 |
| 索引化 + 增量构图 | ✅ | `storage/vector_db.py::_SparseIndex`（SQLite 倒排：sparse 权重 + 正文 2-gram）；`brain/activation.py` 落盘 gzip 缓存 + 按 id/正文 hash 增量 |
| 便携发行 + GPU 探针 | ✅ 已出包 | `dist/super-brain/`（onedir，9.6GB 含 3.35GB 权重）；冻结包实测 `verdict=GPU_OK`、`all_on_gpu=true`、`sparse_enabled=true`。流程见 §8 |
| 多 Agent 共享（外置大脑） | ✅ | 所有 Agent 写同一命名空间、召回不过滤（`gateway/mcp_server.py`）；stdio 进程经 `mcp_bridge.json` 转发给桌面内核，不各自开库 |
| 入口排队（中枢裁决） | ✅ | `gateway/http_adapter.py`：`queue.Queue` + 唯一工作线程 FIFO，`queue_depth` 显示在接口页 |
| 一键复制 MCP 配置 | ✅ | 接口页「复制 MCP JSON」：`streamable-http`（`http://127.0.0.1:8766/mcp` + `X-Super-Brain-Bridge` 令牌头，粘进 Agent 的「添加 MCP 连接」）或 `stdio`（本机启动器）。不再输出大段说明文字 |
| MCP over HTTP 端点 | ✅ | `HTTPAdapter._handle_mcp`：`POST /mcp` 支持 initialize / notifications / tools/list / tools/call / ping，返回 `Mcp-Session-Id`；认证接受 `X-Super-Brain-Bridge` 或 `Authorization: Bearer <桥接令牌>` |
| 桥接令牌持久化 | ✅ | 令牌落 `%APPDATA%/super-brain/mcp_token.txt`，服务启动优先复用 —— 重启软件后已复制的 JSON 不会失效（此前每次启动都换新 token，是「老是 token 错误」的根因） |
| 批量导入快速通道 | ✅ | `import_memories(fast=True)` → `remember(use_llm=False)`：跳过 LLM 抽取与语义合并裁决（仍保留丘脑准入/SimHash/embedding/六引擎/事件日志），导入走后台线程 + 进度信号 |
| 单一数据目录 | ✅ | 设置页「数据保存位置」一处；改路径=写配置+关内核+复制+重启+回读，失败回滚；旧文件名(`r.db`)也能正确迁移 |
| 托盘驻留 + 开机自启 | ✅ | `tray.py` / `autostart.py`：叉窗口只隐藏、单实例唤回、托盘「完全退出」；自启命令带 `--tray` |
| 首页资源仪表 + 模型运行总览 | ✅ | `system_stats.py`（psutil + nvidia-smi，2s 采样）；星云页顶部 CPU/内存/显卡；模型页顶部三通道 × 本地/远程 × 设备 × 状态 |
| 窗口自适应屏幕 | ✅ | `MainWindow._fit_to_screen()`：按可用区收敛并居中；接口页改滚动区（原最小高度 859 会把窗口顶到 937） |
| v3 事件驱动内核 | ✅ 与 v2 并行可回退 | `v3/`：daemon(HTTP 8768 + WS 8767) 只受理（journal group commit + fsync → inbox 租约队列 → 202），无状态 worker 后台 claim 复用 v2 认知链路；supervisor Job Object 防孤儿；`v3/web/` WebGL 星云由 daemon 直接 serve（见 ADR-019） |
| Flutter 3D 星云客户端 | ✅ 定稿（已编译出包） | `flutter_app/`：WS RPC（`ws://host:8767/ws`，12s 超时降级 HTTP）连无头后端；纯 Dart 3D 星云（球壳/螺旋臂布局，CustomPainter 批量 drawPoints，3000+ 节点）；`v3/rpc.py` + `v3/rpc_ext.py` 是 RPC 分发器（见 ADR-020/021）。仓库内**无编译产物**，本机也未安装 Flutter SDK |
| 无头后端（单进程统一内核） | ✅ 端到端验收通过 | `headless_server.py`：`python -m headless_server` 一份 `build_app()` 同时开 8766 `/mcp`（含持久令牌 + stdio 启动器）、8768 `/v1/*`、8767 `/ws`；`V3Daemon(app=...)` 注入共享 app；配置/启动器/业务服务下沉到 `core/user_config.py`、`gateway/bridge_launcher.py`、`brain/memory_service.py`（见 ADR-021） |
| Flutter 功能补齐 | ✅ | 新增记忆检查器（纠正/遗忘/敏感/审计）、事件时间轴、批量导入、全量配置（动态表单 + 预设 + Core Memory + MCP 配置）；`v3/rpc_ext.py` 补齐 `memory.*`/`timeline`/`import_memories`/`config.*`/`core_memory.*`/`gateway.status`；召回改走真实六通道而非 2-gram |
| 后端自动拉起（进程分离） | ✅ | `backend_launcher.dart` 探活 `/v1/health` → `Process.start(..., detached)` 拉起 `python -m headless_server` → 轮询 30s；启动命令/目录存 `flutter_prefs.json`，「全量配置」页可改 |
| 端到端验收脚本 | ✅ 全 PASS | `scripts/verify_headless.py`：临时目录隔离，验证 MCP 六工具、异步写入、全部 RPC（含此前 process 模式必失败的 4 个）、真实召回、导入、纠正、热重启换核后 MCP 仍可用 |
| 后端随 Flutter 安装包部署 | ✅ 链路就绪（产物待构建） | `scripts/build_portable.py --headless` 生成 `backend_entry.py` + `super-brain-backend.spec`（excludes PySide6/keyring/psutil/ui.desktop），产物 `headless-server.exe`；`installer/build.ps1` 组装 `super_brain.exe` + `backend\`，`setup.iss` 安装后只启 Flutter；`backend_launcher.dart` 优先用安装目录 exe；一键脚本 `scripts/build_full_release.ps1`（`-InstallSdk` 可 clone Flutter stable）。见 ADR-022 |
| Qt 隐藏调试入口 | ✅ 默认不加载 | `python -m ui.desktop` 是唯一入口；源码约 7,000 行 / 630 KB，真正开销在 PySide6 200–300MB，已排除出发行包；`-IncludeDebugConsole` 才额外打包。与后端互斥（Kùzu 单写者） |
| 本机 SDK 事实 | ✅ 已装齐 | Flutter 3.47.9 / Dart 3.13.7 在 `D:/flutter`；VS BuildTools 已装（MSVC x64）；`flutter build windows --release --no-tree-shake-icons` 通过，产物 `flutter_app/build/windows/x64/runner/Release/`；绿色版验证目录 `D:/SuperBrain/` |

启动：`python -m ui.desktop`；改完代码重启：`powershell -File scripts/restart_frontend.ps1`。
离屏整窗截图：`python scripts/desktop_screenshots.py` → `data/screenshots/`。
演示数据：`python scripts/seed_demo.py`（先关桌面窗口，Kùzu 单进程写）。

## 2. 环境事实（已实测，别重复摸索）

- 解释器：项目内 `.venv`（实际 **Python 3.12.10**，base=`D:\Python312`，见 `.venv\pyvenv.cfg`）。系统 PATH 上的 `python` 与 venv 不是同一套依赖，**别用它跑任何东西**（`-m ui.desktop` 会拉起另一个实例抢 Kùzu 锁）。
- 已装：`duckdb 2.0.0` / `lancedb 0.41.0` / `kuzu 0.11.3` / `lmdb 3.0.0` / `hypernetx 2.4.3` / `pydantic 2.14.2` / `PySide6-Essentials 6.12.0` / `keyring 25.7.0` / `mcp 1.30.0`（**必须 <2**，2.x 改了 API）/ `pytest 9.2.0`。锁定见 `requirements.txt`。
- 已装（阶段 1~6 运行时）：`onnxruntime-gpu 1.19.2`（providers 含 `TensorrtExecutionProvider`/`CUDAExecutionProvider`/`CPUExecutionProvider`）、`llama-cpp-python 0.3.36 (cu124)`（`llama_supports_gpu_offload() == True`）、`tokenizers`、`python-dotenv`、`onnx`/`numpy`。
- 未装（按降级实现）：`fastapi`、`nicegui`、`apscheduler`、`GitPython`（git_worker 走 CLI 兜底）。
- 本机算力：**GTX 1060 / SM 6.1 / 6143 MiB / 驱动 581.57（CUDA Driver 13.0）**。三模型权重已落位 `data/models/`：`BAAI/bge-m3/model.onnx(+_data)`、`bge-m3/sparse_head.onnx`、`bge-reranker-v2-m3/model.onnx`、`qwen3-0.6b-q8_0.gguf`（合计约 3GB）。安装步骤与 CUDA DLL 归集记录在 `requirements-optional.txt`，重装轮子后按注释把 `nvidia\\cuda_runtime`/`nvidia\\cublas` 的 DLL 拷回 `llama_cpp\\lib`（另有一份暂存 `data\\wheels\\cuda_dlls\\`）。
- `--probe-models --json` 的 `verdict` 取值：`GPU_OK` / `GPU_PARTIAL` / `MODEL_MISSING` / `CPU_ONLY` / `GPU_FELL_BACK_TO_CPU`（判定条件见 `main.py::probe_models`）。**判断 CUDA 是否生效只认本探针**，`--self-check` 只构造对象，证明不了加载。
- 用例基线：**141 passed**（2026-11-08 全量实测）；`websockets==17.4` 已装（v3 WS 服务端延迟 import，缺它 `test_v3_rpc` 会失败）。
- 本机**没有 Flutter/Dart SDK**（PATH 与 C:/D: 常见路径均无），`flutter analyze` / `flutter build windows` 尚未跑过；`analysis_options.yaml` 已移除未声明的 `flutter_lints` include，避免 analyze 直接报错。`pubspec.lock` 记录 Dart `>=3.11.0-0` 而 `pubspec.yaml` 写 `>=3.2.0`，若 `flutter pub get` 报约束不匹配，删 lock 重新生成。
- GitHub：gh CLI 已登录账号 **FFGG10-29**；私库 `github.com/FFGG10-29/super-brain-memory`（remote origin 已配）。事件同步库建议另建（代码库与事件总线分开）。
- Qt 测试/离屏渲染必须先设 `QT_QPA_PLATFORM=offscreen`（`tests/test_desktop.py` 模块顶部已设）。
- Qt offscreen 环境里**不要 spawn 子进程**（句柄继承 WinError 50）；字体库为空，截图脚本须注入 `msyh.ttc`。
- 用户配置：`%APPDATA%/super-brain/config.json` 存差异补丁；测试经 conftest 的 `SUPER_BRAIN_CONFIG_DIR` 重定向（曾因未隔离污染真实配置导致前端启动崩溃——已修，别回退）。密钥只进钥匙串。
- 存储引擎开关语义：relational 恒开；关闭=空实现占位，事件可重放重建。
- shell 是 **PowerShell**：别用 `cd /d`；命令行内嵌 Python 代码时避免三引号字符串（PowerShell 转义会碎），改写临时脚本文件执行。

## 3. 跑测试的正确姿势（沙箱限制，必看）

```powershell
Set-Location -LiteralPath 'B:\BaiduSyncdisk\desktop\VSProject\super-brain-memory'
& '.\.venv\Scripts\python.exe' -m pytest tests -q --basetemp="$env:TEMP\pb_$(Get-Random)"
```

- `--basetemp` 必须指向当次不存在的新目录（safe-delete 钩子会拦截 rmtree）。
- 自检：`& '.\.venv\Scripts\python.exe' main.py --self-check`（实测冷启动 0.5~0.9s）。

## 4. 引擎 API 已核实（0.41/0.11 与旧文档不同，照此写）

### lancedb 0.41
- `db.list_tables()`（`table_names()` 已废弃）；稠密列必须 `pa.list_(pa.float32(), dim)`。
- 检索：`tbl.search(vec).metric("cosine").where("is_active = true", prefilter=True).limit(k).to_list()`。
- **没有"无查询的 where 扫描"**：稀疏通道先 `to_arrow()` 全量取行再内存打分。

### kuzu 0.11
- 一个进程一个 `kuzu.Database`（**桌面实例与脚本不能同时打开**，灌数据先关窗口）；超图复用同一 Database。
- 参数 `$name` 字典传入；`get_as_df()` 依赖 pandas（未装）→ 用 `get_column_names()` + `has_next()/get_next()`。

### duckdb 2.0
- 单写多读：单连接 + `conn.cursor()` 每线程隔离 + 写锁。

## 5. 已确立的关键设计约定（改动前先读 .memory/DECISIONS.md）

- 记忆自身的 **PAD 不做基线平滑**；平滑只用于 Agent 情绪基线（ADR-007）。
- 存储层契约是 `Mapping`，实体只活在 `models/`、`brain/`（ADR-002）。
- 投影失败不回滚事实；归档不参与常规召回；冷静期不改写已存 PAD。
- 语义合并兜底安全：LLM 失败/裁决非法一律 add，绝不静默丢信息（ADR-010 配套）。
- FTS 中文整串查询命中 0 → 兜底 LIKE；语义合并候选探测用 **4 字滑窗片段**（改写句无公共前缀）。
- Qt `QGraphicsView.scene`/`QGraphicsItem.data` 是保留方法名，项目用 `view_scene`/`node`（ADR-008）。
- `Switch.setChecked` 必须同步滑块位置（否则初始渲染看起来是关）。

### v2.2 新增约定（用户控制与可视化，改动前先读 ADR-011~014）

- **纠正/遗忘不新增事件类型**：纠正 = 新版本 `ingest` + 旧版本 `supersede`（payload 带 before/after/actor/reason）；遗忘 = `tombstone`。绝不原地改 payload（主库 upsert 与 `vector.upsert` 都是整行替换，改了就丢审计原值）。
- **不做物理删除**：遗忘只从推理路径移除（`deactivate` + `vector.delete`），主库留封存行、journal 留事件。物理清除会让跨设备重放把已删记忆复活；合规硬清除需另建 tombstone purge 流（须同步清向量/图谱/时序/KV）。
- **索引与缓存是派生**：`_SparseIndex`（SQLite 倒排）与 `activation_cache.json.gz` 都可随时删除重建；签名/行数不符即整表重建，索引异常退回全表扫描，结果必须与全扫一致。
- **敏感记忆是「上下文遮蔽」不是加密**：KV 只存 `sensitive` 标记，正文仍在库内（同理不参与跨设备同步的"删除"）；列表/召回默认显示 `••••••`，检查器里显式揭示。
- **UI 重活一律走 `backend.tasks.submit()`**：单线程池 + `QueuedConnection` 回主线程，后台忙时状态刷新直接跳过（不排队等锁）。
- **星云布局保持纯确定性**：坐标只由 id + tier + memory_type + uuid7 时间戳决定，跨设备/截图可复现；不要引入随机数或运行期状态。

## 6. 下一步

0. **Flutter 单前端收尾（验收后再删 Qt）**：装 Flutter SDK → `flutter pub get` → `flutter analyze`
   → `flutter build windows --release` → 按 `flutter_app/README.md` 十四页逐页验收
   （检查器纠正/遗忘、时间轴、批量导入、全量配置保存 + 重启内核、后端自动拉起）。
   全过后删 `ui/desktop/`，改写 `tests/conftest.py` 与 3 个混合测试文件、
   `scripts/build_portable.py` 入口改 `-m headless_server`、清 `requirements.txt`
   的 PySide6/keyring/psutil，并同步 README/.memory。硬阻塞清单见 ADR-021。
1. **先在本机补探针结论**（关掉桌面窗口，Kùzu 单进程锁）：`main.py --probe-models --json`，
   把 `verdict` / 三段 `on_gpu` 回填 `ACCEPTANCE.md`；若 `GPU_PARTIAL` 按 llm → embedder → reranker 定位。
   判定要 `verdict == GPU_OK` + `all_on_gpu == true` + `embedder.sparse_enabled == true`；
   `--self-check` 只构造对象，区分不了「CUDA 生效」与「根本没加载」。
2. 另一台机器：`git clone https://github.com/FFGG10-29/super-brain-memory` → venv +
   `pip install -r requirements.txt` → 按 `requirements-optional.txt` 的 CUDA 段装 onnxruntime-gpu 与
   llama-cpp CUDA 轮子 → 权重放 `data/models/` → 应用「独显」预设 → 探针。
3. 设 `GW_MCP_TOKEN_SECRET`；跑一次「快照 → 删库 → 恢复」演练。
4. 事件同步库：可另建 `super-brain-memory-events` 私库，把 `SYNC_REPO_URL` 填进「同步与备份」页（代码库与事件总线分开）。
5. WorkBuddy/CodeBuddy/DSH 的 mcp.json 指向 `python -m gateway.mcp_stdio`（见 `gateway/mcp_stdio.py` docstring）。
6. 换机实测（本机已出包，见 §8）：整目录拷到目标机器 → `super-brain-cli.exe --probe-models --json`
   要求 `GPU_OK`；无 N 卡 / 驱动过旧的机器应优雅落 CPU（`CPU_ONLY` 或 `GPU_FELL_BACK_TO_CPU`），不能崩。
   另可跑 `scripts/verify_portable.py` 做「改目录 / 无 `.env` / DLL 不在 venv」条件下的复核。
7. 单 exe（onefile）不建议：onnxruntime/llama-cpp/kuzu 的 hidden imports 与 CUDA DLL 解压拖慢启动，onedir 足够。

## 7. 已知坑

- `sqlite3` FTS5 `unicode61` 中文整串查询命中 0 → LIKE 兜底查 `body` 列。
- `sorted()` 混排中英文 ASCII 在前，测试比较集合别比较列表。
- 不要给 `.gitignore` 加 `.memory`；该目录必须提交。
- pytest 进程内 `SUPER_BRAIN_CONFIG_DIR` 是**进程级共享临时目录**，测试写入会跨用例可见。
- 冻结态下 CUDA DLL 必须在 **import `llama_cpp` / `onnxruntime` 之前** 调 `prepare_cuda_dll_path()`（`main.py` 顶部已做），否则 cuDNN 子库找不到会静默回落 CPU，表现为「配了显卡却跑 CPU」。
- 冻结态 `DATA_DIR` 指向用户可写目录（`%APPDATA%/super-brain/data`），**绝不写 `_MEIPASS` 或安装目录**；`PROJECT_ROOT` / `RESOURCE_ROOT` 已分开，别再硬编码 `Path(__file__).parent`。
- 配置键改名：`model.llm_model` → `model.llm_gguf_file`（文件名），`.env` 里旧的 `MODEL_LLM_MODEL` 不再生效。
- 子模型（`ModelConfig` / `SyncConfig` / `GatewayConfig`）的 `env_file` 不会自动读 `.env` → `config.py` 用 `load_dotenv(override=False)` 注入 `os.environ`，且顶层 `extra="ignore"`，否则 `.env` 的 `MODEL_*` 会被判为多余字段。
- 事件时间轴是导航末页（索引 10），窗口关闭必须 `timeline_page.stop()`（停轮询定时器）+ `backend.tasks.wait()`，否则退出时后台线程还在碰六引擎。
- 6GB 显存同时挂三模型**实测可 GPU_OK**（bge-m3 2.2GB + reranker 570MB + qwen 640MB + CUDA context），但余量很小：长上下文批量推理仍可能 OOM 回落 CPU。出现 `GPU_PARTIAL` 时按 llm → embedder → reranker 顺序定位，可换 int8 精排或临时关精排。

- **用户 config.json 会覆盖代码里显式构造的 BrainConfig**：`DesktopBackend.start()` 会 `apply_overrides(load_overrides())`，所以脚本/测试必须先把 `SUPER_BRAIN_CONFIG_DIR` 指向临时目录（`scripts/desktop_screenshots.py` 已这么做），否则临时库会被换成真实 `data/`，触发「向量维度 1024 vs 8」。
- 更新 `dist/` 用 `robocopy build\pyi_outN\super-brain dist\super-brain /E`：`Copy-Item -Recurse` 曾把 `_internal` 内容拍平到根目录（多了一层通配符展开）。
- 主窗口尺寸受**所有页面的最小高度**约束（QStackedWidget 取最大值）：原来接口页最小 859，把窗口顶到 937，笔记本上标题栏飞出屏幕。加滚动区前先量 `page.minimumSizeHint()`。
- `psutil` 已进 `requirements.txt`（首页仪表）；显卡走 `nvidia-smi`，非 N 卡显示「无 N 卡 / 不可用」。
- **无头后端与 Qt 桌面端不能同时跑**：两者都 `build_app()` 打开同一 Kùzu 库，单写者会抢锁（先关桌面窗口再起 headless）。
- **热重启必须先关旧内核再 `build_app()`**：`HeadlessServer.restart_kernel()` 已统一换 toolkit/registry/worker；只换 `daemon.app` 会让 MCP 指向已关闭的旧引擎。
- **三个端口各司其职**：8766 `/mcp`（Agent）、8768 `/v1/*`（Flutter HTTP 兜底 + 探活）、8767 `/ws`（RPC）；改端口要同时改 `flutter_prefs.json` 与后端启动参数。

## 8. 发行包（实测用）

一键流程（顺序不能换）：

```powershell
.\.venv\Scripts\python.exe scripts/build_portable.py                 # 归集 CUDA DLL + 权重，生成 spec/入口
.\.venv\Scripts\python.exe -m PyInstaller super-brain.spec --noconfirm  # 约 4.5 分钟
.\.venv\Scripts\python.exe scripts/build_portable.py --trim dist/super-brain        # 删重复 DLL + TensorRT
.\.venv\Scripts\python.exe scripts/build_portable.py --link-models dist/super-brain # 模型硬链接（省 3GB 复制）
```

产物 `dist/super-brain/`（9.6GB）：

- `super-brain.exe` → GUI（无控制台）；`super-brain-cli.exe` → `--self-check` / `--probe-models`（GUI 子系统没有 stdout，必须用这个）。
- `_internal/`：`cuda_bin/`（补缺，重复项已被 trim 删）、`onnxruntime/`、`llama_cpp/lib/`、`models/`（硬链接到 `build/portable_stage/models`）。
- 可写数据落 `%APPDATA%\super-brain\data`；配置文件读 `%APPDATA%\super-brain\config.env`；模型优先 `_internal/models`。
- 整目录复制到别的机器即可运行；**首次启动是空库**，用「记忆管理 → 批量导入」或设置页配好模型后写入。

本机冻结包实测（2026-11-06）：

- `super-brain-cli.exe --self-check --json` → 7 个引擎全 `ok=true`，冷启动 0.57s。
- `super-brain-cli.exe --probe-models --json` → `verdict=GPU_OK`、`all_on_gpu=true`、
  llm `active_gpu_layers=-1`、embedder/reranker 均 `CUDAExecutionProvider`、`sparse_enabled=true`、`cuda_dll_complete=true`（驱动 API 13.0）。
- `super-brain.exe` 正常出窗口（十一页导航齐全），数据落 `%APPDATA%`，不写安装目录。

---

## 9. 2026-11-08 增补：Flutter 完全替代 Qt 的 6 项能力（已落地）

用户要求 Flutter 完美替代 Qt，ADR-021 列的 6 项差异全部写成可运行代码，导航 **14 → 18 页**。

| 能力 | 后端 | Flutter |
|---|---|---|
| CPU/内存/显卡仪表 | `core/system_stats.py`（psutil + nvidia-smi，2s 守护线程） | `system_gauges.dart`：顶栏环形仪表 + `硬件仪表` 大表页 |
| 召回归因 | `recall` 已有 `audit.counts/injected/weights/similarities` + `injection` | `analytics.dart::RecallAnalyticsPage`：通道分解 / 单条归因 / 注入上下文 |
| PAD 认知分区 | `pad.distribution`（主库 pad_p/a/d） | `PadAnalyticsPage`：P–A 散点 + 分类型/分层均值 |
| 2D 语义环联动 | `graph.projection`（Qt `_semantic_position` 等价，确定性坐标） | `Graph2DPage` + `MemoryInspectorPage.initialId` + 3D 星云回跳 |
| 托盘/单实例/自启 | `core/win32_tray.py`（ctypes Shell_NotifyIconW + WS 广播）、`core/desktop_features.py`（winreg） | `win32.dart`（端口锁 + user32 FFI）、`win32_window.cpp` 拦截 WM_CLOSE、`桌面能力` 页 |
| 钥匙串 + 数据迁移 | `SecretStore`（keyring→DPAPI 兜底）、`migrate_data()` + `runtime_paths.data_dir()` 读 `data_pointer.json` | `system_features.dart` 页 |

关键坑（新增）：
- **冻结包 excludes `ui.desktop`**：后端能力必须全部下沉到 `core.*`，不能在 `v3/rpc_ext.py` 里 import `ui.desktop.*`，否则发行包启动即 ModuleNotFoundError。
- **hiddenimports**：`scripts/build_portable.py` 的 `--headless` spec 已补 `core.system_stats` / `core.desktop_features` / `core.win32_tray` / `psutil` / `winreg`（函数内惰性 import 静态分析会漏）。
- **ctypes 类型**：`ctypes.wintypes` 没有 `HCURSOR/HBRUSH/HMENU` 别名，所有 Win32 句柄统一用 `wintypes.HANDLE`；`GetModuleHandleW` 必须显式设 `restype=HINSTANCE`，否则 x64 指针被截断成 32 位。
- **零 pub 依赖**：`dart:ffi` 调 `user32.dll`，UTF-16 缓冲区用 `msvcrt.dll` 的 malloc/free；没引入 `package:ffi` 或托盘/window_manager 插件。
- **数据迁移 pointer 同源**：`desktop_features._pointer_path()` 与 `runtime_paths._data_pointer()` 都必须用 `user_dir()/data_pointer.json`，不要用 `SUPER_BRAIN_CONFIG_DIR`（两套目录会不一致）。

验证：`flutter analyze` **No issues found**、`pytest tests -q` **141 passed**、新增 RPC 隔离探针 9 方法全 OK（PAD 均值/投影确定性断言通过）、`py_compile` 全过、托盘 ctypes start/stop 实测通过、采样真实返回 GTX 1060/49°C。
唯一剩余阻塞：**MSVC 需管理员 UAC**，`flutter build windows --release` 与 C++ `WM_CLOSE` 拦截尚未真机编译验收。

---

## 10. 2026-11-09 增补：Flutter 前端定稿（已出安装包）

### 10.1 导航结构（最终形态）

左栏 **7 组主导航**，组内页面放顶栏下的 `ChoiceChip` 子标签：

| 组 | 页面（全局索引） |
|---|---|
| 星云视图 | 3D 星云 `0` |
| 记忆工作台 | 记忆管理 `1` · 召回测试 `2` · 批量导入 `14` |
| 记忆洞察 | PAD 认知分区 `4` · 2D 语义环 `5` · 记忆检查器 `12` · 事件时间轴 `13` |
| Agent 上下文 | 上下文状态 `18` · 运行时记忆 `19` · 项目档案 `20` · 记忆空间 `21` · 技能管理 `22` |
| 运行监控 | 硬件仪表 `3` · 系统概览 `6` · 展示数据 `7` · 模型配置 `8` · 接口与端口 `9` |
| 系统设置 | 设置 `11` · 全量配置 `15` · 桌面能力 `16` · 同步与备份 `10` |
| 关于 | 关于 `17` |

- 设置页内部再分 3 个子标签（连接 / 外观 / 诊断），星云参数只在「外观」出现一次；
- 「全量配置」按 12 组分类，常用组置顶、高级组默认折叠，**鼠标悬停每个字段有中文说明**
  （`flutter_app/lib/config_docs.dart` 由 `ui/desktop/widgets.py` 的 FIELD_LABELS/HINTS 用 AST 抽取生成，128/128 全中文）；
- 「记忆管理」按 `memory_type` / `tier` 分类 Chip，UUIDv7 时间戳解析后**最新记忆置顶**。

### 10.2 两个致命问题（根因与修法，务必记住）

**① 首屏 0 节点（记忆 1000 / 节点 0）**

- 根因：`main.dart` 里 `BackendLauncher.ensureRunning()` 是 `unawaited`，UI 首帧立刻构建；
  `NebulaPage.initState → client.graph()` 此时后端还没监听端口 → WS 连不上 → 走 HTTP 兜底 →
  连接被拒 → 返回 `{'error': ...}`。
  而 `BrainGraph.fromJson()` 对没有 `nodes` 的 Map **不报错、直接产出空图**，
  `_load()` 又把它当成功 → 状态栏「0 节点（默认 200）」，之后再也不会重试。
- 修法（`brain_client.dart` / `nebula_page.dart`）：
  1. `graph()` 带 3 次重试，且「没有 nodes 字段」按失败处理并记 `lastGraphError`；
  2. `_load()` 空结果不当成功，900ms 重试最多 10 次，UI 显示「等待后端返回记忆节点…（第 n 次重试）」；
  3. `_onEvent('connected')` 时若仍是空图，自动重新加载（WS 比端口晚就绪的兜底）；
  4. `_httpFallback` 对 `{"result": {...}}` 的 **Map 结果也要拆包**（旧版只拆数组，
     这正是「后端明明有 1261 条、星云却是 0 节点」的另一条路径）；
  5. 加 `debugPrint('[nebula] graph ok: raw/parsed/edges/clusters')` 与坐标范围检查，
     以后同类问题看日志即可定位，不用再猜。

**② 节点糊成「一堆光点」**

- 根因：`%APPDATA%\super-brain\flutter_prefs.json` 里 `nebula.star_size = 8.0`（默认 3.2）。
  渲染时近景点直径 ≈ `starSize × 1.15` → 9px 光球，1261 个叠在一起就是肥皂泡。
- 修法：`_styleFromPrefs()` 把 `starSize` 钳在 **1.4~4.5**；点直径按 `0.58×` 收小；
  bloom 只在最近的一小撮点上、alpha 0.06；结构改由**每簇一团 tint 色径向云气**承担（alpha 0.20）。

### 10.3 星云视觉（定稿参数）

- **布局**：内容 2-gram 贪心聚类 → 12 星团；簇内 66% 双致密核（近高斯）+ 34% 对数螺旋悬臂；
  跨簇弱连线；世界坐标归一化到 `targetExtent = 350`（否则 1261 节点时簇心飞到 ±800，屏幕只剩零星几粒）。
- **着色**：按 `memory_type` 分类上色——`fact` 金橙 `0xFFFFB454`、`scenario` 紫罗兰 `0xFF9B72FF`、
  `code` 青蓝 `0xFF3BE8FF`、`emotion` 紫红 `0xFFFF3F86`、`profile` 暖金 `0xFFFFD166`；
  `fact` 内再按内容正则识别代码类；星团主色 = 簇内多数分类。
  注意：**不要用后端 `is_emotion`（pad_a>0.6）兜底**，它会把 90% 节点染成紫红。
- **中枢**：白色恒星，`_drawCore` 整体压暗约 30%（核心 0xC8 白、半径 27），星芒变细变短。
- **黑洞**：半径整体缩到原来的 ~62%（视界 38 / 光子环 44 / 吸积盘 96），移到 `(0.88w, 0.66h)`，
  只作角落景深道具。
- **连线**：`show_edges` 默认关；开启后按 6 个相位桶用 `sin(spinTime*1.7 + phase)` 做透明度/线宽呼吸。
- **性能**：LOD 三档（zoom<0.72 只画点）、全量模式 `memories(limit=100000)` 兼容路径 + 500/批渐进渲染，
  500 条后 `recluster()` 一次成团。

### 10.4 图标树摇（两个菜单图标空白）

- 现象：左栏「记忆洞察」「Agent 上下文」两项图标空白，其余正常。
- 根因：`flutter build windows --release` 默认 `--tree-shake-icons`，
  这两个图标**只**出现在 `const List<Map<String, Object>>` 里（动态取值 `_groups[g]['icon']`），
  静态收集不到 → `MaterialIcons-Regular.otf` 被裁成 5.6KB，对应字形被删。
- 修法：构建命令加 **`--no-tree-shake-icons`**（字体 1.6MB，全字形）；已在本地构建流程采用。

### 10.5 MCP 能力面（Agent 直接可用）

- `gateway/tool_catalog.py` 是**单一真源**：53 个工具定义 + `RPC_MAP` + `manifest` + `resource_list/read`
  + `dispatch_standalone`（stdio 独占模式用）。
- 6 个 resources：`brain://runtime/memory`、`brain://project/archive`、`brain://spaces`、
  `brain://skills`、`brain://manifest`、`brain://health`。
- 鉴权头是 **`X-Super-Brain-Bridge`（不能写 Bearer）**；HTTP 端点 `http://127.0.0.1:8766/mcp`。
- `recall_memory` 默认走只读投影 2-gram（毫秒级），`deep=true` 才走六通道（30~45s），返回值带 `recall_mode`。
- 分层上下文 `core/context_layers.py`：运行时记忆 / 项目档案 / 记忆空间 / 技能 四层，落盘 `Data/context`。

### 10.6 发行

- 前端产物：`flutter_app/build/windows/x64/runner/Release/`（`super_brain.exe` + `data/app.so`）。
- 部署验证位置：`D:\SuperBrain\`（绿色版），后端 `D:\SuperBrain\backend\headless-server.exe`（PyInstaller 冻结，2026-11-09 00:00）。
- **注意**：该冻结后端尚未包含 `graph.chunk` RPC，全量模式自动降级到 `memories(limit=100000)`；
  下次重打后端即可补上（`v3/rpc.py::_graph_chunk` 已在源码里）。
- 安装包：`scripts/installer/setup.iss` + ISCC（`lzma2/normal`，关闭固实压缩以降低内存，
  避免与后端抢资源时 `ucrtbase.dll 0xC0000409`）；分卷按 `release/` 里的既有方案（每卷 < 2GB + sha256）。
