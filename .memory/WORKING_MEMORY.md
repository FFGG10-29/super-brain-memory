# 工作记忆 · 当前状态

> 最后更新：2026-11-06 ｜ 当前阶段：**v2.2 桌面体验/多 Agent 共享 + v3 事件驱动内核（并行落地）已实机验证，139 用例全过；便携包已出（9.6GB，冻结态 GPU_OK）**

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
| 一键复制 MCP 配置 | ✅ | 接口页「复制 MCP JSON」：`streamable-http`（`http://127.0.0.1:8766/mcp` + Bearer 令牌，粘进 Agent 的「添加 MCP 连接」）或 `stdio`（本机启动器）。不再输出大段说明文字 |
| MCP over HTTP 端点 | ✅ | `HTTPAdapter._handle_mcp`：`POST /mcp` 支持 initialize / notifications / tools/list / tools/call / ping，返回 `Mcp-Session-Id`；认证接受 `X-Super-Brain-Bridge` 或 `Authorization: Bearer <桥接令牌>` |
| 桥接令牌持久化 | ✅ | 令牌落 `%APPDATA%/super-brain/mcp_token.txt`，服务启动优先复用 —— 重启软件后已复制的 JSON 不会失效（此前每次启动都换新 token，是「老是 token 错误」的根因） |
| 批量导入快速通道 | ✅ | `import_memories(fast=True)` → `remember(use_llm=False)`：跳过 LLM 抽取与语义合并裁决（仍保留丘脑准入/SimHash/embedding/六引擎/事件日志），导入走后台线程 + 进度信号 |
| 单一数据目录 | ✅ | 设置页「数据保存位置」一处；改路径=写配置+关内核+复制+重启+回读，失败回滚；旧文件名(`r.db`)也能正确迁移 |
| 托盘驻留 + 开机自启 | ✅ | `tray.py` / `autostart.py`：叉窗口只隐藏、单实例唤回、托盘「完全退出」；自启命令带 `--tray` |
| 首页资源仪表 + 模型运行总览 | ✅ | `system_stats.py`（psutil + nvidia-smi，2s 采样）；星云页顶部 CPU/内存/显卡；模型页顶部三通道 × 本地/远程 × 设备 × 状态 |
| 窗口自适应屏幕 | ✅ | `MainWindow._fit_to_screen()`：按可用区收敛并居中；接口页改滚动区（原最小高度 859 会把窗口顶到 937） |

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
- 用例基线：**134 passed**（桌面 26 / 用户控制 6 / 增量与索引 4 / 配置中心 16 / MCP 6）。
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
