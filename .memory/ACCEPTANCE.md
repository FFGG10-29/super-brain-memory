# 七阶段验收台账

> 最近一次全量回归（2026-11-06）：**123 passed**（桌面 22 / 用户控制 6 / 增量与索引 4 / 配置中心 13），`verify_baseline.py` 基线成立，`main.py --self-check --json` 退出码 0。
> 跑法：`pytest tests -q --basetemp="$env:TEMP\pb_$(Get-Random)"`（沙箱 safe-delete 限制，见 WORKING_MEMORY §3）

## 阶段 0 地基 ✅

| 验收项 | 状态 | 证据 |
|---|---|---|
| config 全量外置 | ✅ | `config.py` 九组 Settings，阈值/权重/端口/路径全覆盖 |
| 六引擎连接器 + 建表 + 健康检查 | ✅ | `main.py --self-check --json` 七项 ok=true |
| thread-local + WAL | ✅ | `ThreadLocalPool`；`PRAGMA journal_mode=wal` 用例 |
| 三模型懒加载降级 | ✅ | 构造不加载权重；`resolve_onnx_providers` 断 CUDA 落 CPU |
| interfaces 全量 | ✅ | 三模型 + 六引擎 + EventLog/Projector + HealthCheckable |
| HLC / UUIDv7 | ✅ | 回拨/计数器溢出/远端合并用例 |
| 各引擎连通 / 冷启动 <30s / 断 CUDA | ✅ | 实测 1.09~3.65s；`scripts/verify_baseline.py` 全 OK |

依赖基线：`requirements.txt` 精确锁定 14 个包（含 hypernetx 2.4.3），`requirements-optional.txt` 存阶段 1~6 可选依赖。

## 阶段 1 写入流 ✅

| 验收项 | 状态 | 证据 |
|---|---|---|
| 重复写入零冗余 | ✅ | 精确键 + SimHash 双去重，count 仍为 1 |
| 矛盾事实封存 + 图谱 supersedes 边 | ✅ | is_active=0 + valid_to 打点，SUPERSEDES=1 |

## 阶段 2 召回流 ✅

| 验收项 | 状态 | 证据 |
|---|---|---|
| 六路 → RRF → 精排 → 分层配额 → 注入 | ✅ | `brain/prefrontal.py` + 6 条用例 |
| golden-set recall@5 / MRR 基线 | ✅ | `eval/golden_set.py`；`test_golden_set_baseline_saved` |
| CPU 单次召回 P95 < 800ms | ✅ | `test_recall_p95_under_800ms`（12 条记忆 / 20 次召回） |
| 各路召回命中率 + 结构化日志 | ✅ | `core/structured_log.py` JSON 行 + access_log 埋点 |

## 阶段 3 生命循环 ✅

| 验收项 | 状态 | 证据 |
|---|---|---|
| FSRS 衰减/强化落库 | ✅ | stability 增长 + decay_event 快照 |
| tier 晋升/降级/归档 | ✅ | `test_tier_sweep_promotes_and_archives` |
| 归档不再被召回 | ✅ | 同上 + phase2 用例 |
| 金字塔固化 / 夜间复盘 / 晨报 | ✅ | `neocortex.consolidate` 主题可追溯；`LifeLoop.nightly().as_brief()` |
| 7 天模拟数据衰减符合模型 | ✅ 近似 | 用 validity/stability 时间旅行替代真实 7 天等待（模型公式确定性） |
| 图谱修剪 / 遗忘清扫 | ✅ 标记式 | `graph_prune` 输出待修剪清单（不物理删边，保证重放一致） |

## 阶段 4 网关多端 ✅（传输层有一处待接线，见末尾）

| 验收项 | 状态 | 证据 |
|---|---|---|
| registry 令牌签发/校验/吊销 | ✅ | HMAC 签名 + 仅存哈希 + revoke 用例 |
| MCP 工具面 6 工具 | ✅ | `MCPToolkit`；save/recall/timeline/profile/conflict/health |
| HTTP / OpenAI 兼容适配器 | ✅ | 标准库 ThreadingHTTPServer，真实端口冒烟用例 |
| 3 类客户端各一条接入串即通 | ✅ | `test_three_client_types_each_one_connection_string`（MCP×2 + HTTP×1） |
| 命名空间隔离生效 | ✅ | group 客户端写 private 被拒 |

## 阶段 5 同步平面 ✅

| 验收项 | 状态 | 证据 |
|---|---|---|
| journal fsync 先于回执 | ✅ | `sync/journal.py`（`os.fsync`） |
| event_store HLC 全序 + 去重 + 游标 | ✅ | `sync/event_store.py` |
| ① 双机互写互见 | ✅ | `test_two_devices_converge_on_conflicting_fact` |
| ② 断网重连零丢失 | ✅ | `test_offline_reconnect_zero_loss`（离线 5 条 + 恢复 2 条） |
| ③ 重放幂等 | ✅ | 重复 apply：replayed=0 / skipped=1，pad_track 不增 |
| ④ 新设备冷启动全量重建 | ✅ | `test_new_device_cold_start_snapshot_plus_tail_replay`（快照 + 尾部事件，PAD 一致） |
| ⑤ 双端同时写矛盾事实 → 收敛一致 | ✅ | 两端活跃集合均为 1 条，SUPERSEDES 均为 1 |
| git_worker pull/push 真实远端 | ⚠ 未联调 | 无仓库/离线时降级 no-op 已测；需配 `SYNC_REPO_URL` + PAT 后实测 |

## 阶段 6 可视与打磨 ✅

| 验收项 | 状态 | 证据 |
|---|---|---|
| 六大面板数据源 | ✅ | `ui/panels/*` 纯函数 + `Dashboard.snapshot()/render()` |
| NiceGUI 页面 | ⚠ 未装 | `render()` JSON 兜底可用；`serve()` 装 nicegui 后启用 |
| 结构化日志 + 命中率 | ✅ | JSON 行 + access_log |
| 一键启停 / 备份 | ✅ | `main.py --serve`（HTTP + 调度 + worker）、`scripts/backup.py backup/restore` |
| 换模型出 eval 对比报告 | ✅ 接口就绪 | `compare(baseline, current)`；真实换模型尚未跑 |

## PAD 情绪记忆全链路 ✅ 8/8

`tests/test_pad_chain.py` 一段用例串起：①生成 ②建模 ③存储 ④检索 ⑤巩固 ⑥生命循环 ⑦同步 ⑧可视化。
另有规则兜底用例（无 LLM 也不断链）与冷静期不改写已存 PAD 用例。

## 桌面前端 PySide6 · LivingMemory 控制台 ✅（2026-11-05 新增，同日重构）

> 启动：`python -m ui.desktop`；离屏预览：`python scripts/nebula_preview.py` → `data/nebula_preview.png`
> 验收：`pytest tests/test_desktop.py -q` → **22 passed**（含真实 socket 端口 + 事件循环启停）

布局（对齐 LivingMemory）：左侧 220px 导航九页（v2.2 扩到**十一页**，新增「模型配置」「事件时间轴」）· 顶栏（空间选择/返回全景/全局搜索/导出/管理数据/全屏）·
中央星云 · 右下迷你地图 + 浮动按钮 · 底部统计状态栏。

| 验收项 | 状态 | 证据 |
|---|---|---|
| 星云节点来自真实记忆库（非随机假数据） | ✅ | `DesktopBackend.graph()` → `ActivationService.build()`；26 条真实写入 → 26 节点 |
| 语义突触线 = 真实余弦相似度 | ✅ | `brain/activation.py`；无 ONNX 时降级字符 2-gram 且 `degraded=True` 明示 |
| LivingMemory 视觉语言 | ✅ | `pages/nebula_page.py`：星尘 `drawBackground`、记忆标签（情绪优先/朝中心避让）、迷你地图视口矩形 |
| 空间切换（全部/各分层/情感记忆） | ✅ | `state` 顶栏下拉 → `DesktopBackend._filter_graph`；`test_spaces_filter_emotion_only` |
| Lerp 光脉冲 / 终点闪烁 / 跟随漂移节点 | ✅ | `test_lerp_pulse_travels_flashes_and_is_destroyed`、`test_pulse_follows_drifting_nodes` |
| 检索 → 中心向 N 条命中齐射 + 高亮标签 | ✅ | 顶栏搜索 / 召回页「在星云中激活」→ `emit_from_center` |
| 双击记忆 → 定位并高亮神经元 | ✅ | `MemoryPage.focus_requested` → `NebulaPage.focus` → `focus_on` |
| 九页功能齐全 | ✅ | 星云 / 记忆管理 / 召回测试 / 系统概览 / 展示数据 / 接口与端口 / 同步与备份 / 设置 / 关于 |
| **端口配置 + 一键启停（真实 socket）** | ✅ | `start_http("127.0.0.1", 0)` → 内核分配端口 → urllib 请求 `/health` 返回 `{"status":"ok"}` |
| 端口/偏好持久化 | ✅ | `SettingsStore.load_prefs/save_prefs` → 用户级 QSettings，并即时回写 `config` |
| MCP 工具清单与接入串 | ✅ | 6 个工具；agent_id + 令牌掩码；curl 示例随端口生成 |
| 同步/快照/导出/调度可操作 | ✅ | `sync_once`（无仓库 no-op）、`create_snapshot` + 列表、导出 JSON/NDJSON、手动跑 circadian 任务 |
| 真实渲染出画面 | ✅ | `test_render_scene_to_image`（两帧像素不同）+ 整窗 `window.grab()` 预览 PNG |
| PAT / SYNC_REPO_URL 前端配置 | ✅ | `SettingsPanel` 密码框；`SettingsStore` 钥匙串 + QSettings |
| PAT 不入库/不入事件/不回显 | ✅ | 内存替身用例断言明文不进 QSettings、状态只显示掩码 |
| 动画/配色/阈值参数全外置 | ✅ | `config.UiConfig` / `ActivationConfig`（帧率、阈值、标签数均可在设置页调整） |

依赖已进 `requirements.txt`：`PySide6-Essentials==6.12.0`、`keyring==25.7.0`。
预览图渲染注意：offscreen 平台字体库为空，脚本会显式注入 `C:/Windows/Fonts/msyh.ttc`，否则中文变豆腐块。

## 配置中心（打包后免重编译）✅（2026-11-05）

> 设置页 = 一键预设 + 全量参数自动表单；差异补丁存 `%APPDATA%/super-brain/config.json`；密钥只进钥匙串
> 验收：`pytest tests/test_settings_center.py -q` → **13 passed**（含本地假 OpenAI 服务器）

| 验收项 | 状态 | 证据 |
|---|---|---|
| 一键预设 ×3（均衡默认 / 性能全开 / 低配省电） | ✅ | `user_config.PRESETS`；lite 预设关向量/精排、拉长同步 |
| 存储引擎开关（是否全打开） | ✅ | `engines_enabled`：relational 恒开（事实源）；其余关=空实现占位，**事件仍在 journal，重开可重放重建**；写入流不中断 |
| 本地模型开关 + 云端大模型 API | ✅ | `model_mode=local/cloud/auto`；OpenAI 兼容 `/embeddings` `/chat/completions`（urllib 零 SDK）；失败自动退本地/规则 |
| 云端 API key 安全 | ✅ | 钥匙串 > 环境变量；不写配置文件/事件日志/界面明文 |
| 全量参数前端可调（免重编译） | ✅ | `ConfigEditor` 自动表单 11 组：存储/模型/召回/FSRS 遗忘/分层/丘脑准入/情感阈值/同步/网关/星云激活/外观；新增 config 字段零 UI 代码 |
| 内核热重启 | ✅ | `DesktopBackend.restart()`：应用用户补丁重建引擎，数据保留 |
| 精排开关 | ✅ | `recall.rerank_enabled=False` 跳过交叉编码（低配省电） |

## 记忆可视化与用户控制（v2.2）✅（2026-11-06）

> 主题：让记忆「可见、可纠正、可遗忘、可审计」，且全部改动仍落在事件溯源纪律内。
> 验收：`pytest tests/test_user_control.py -q` → **6 passed**；`pytest tests/test_incremental_memory.py -q` → **4 passed**；全量 **123 passed**。

| 验收项 | 状态 | 证据 |
|---|---|---|
| 纠正 → 新版本 + 原值审计 | ✅ | `test_correct_creates_new_version_and_preserves_original`：旧行 `is_active=False` 且 content 原值不变、`tx_to` 打点；新行内容/分层生效；`memory_audit()` 含 `supersede`(changed 含 content) + `ingest` |
| 封存 / 遗忘不出现在召回与列表 | ✅ | `test_forget_removes_from_recall_but_keeps_audit`：`tombstone` 事件 + 主库封存行保留 |
| 编辑走事件而非原地改 payload | ✅ | `brain/memory_editor.py` 写入顺序 journal(fsync) → 主库 → 派生库/outbox；可编辑字段白名单 `_EDITABLE` |
| 事件时间轴 + EventType 筛选 | ✅ | `test_event_timeline_and_type_filter`：`event_timeline(types=...)` 过滤正确、`event_types()` 恰 7 类；页面 30min 会话分组 + 3s 轮询 |
| 时间轴/星云 → 检查器双向可达 | ✅ | `MainWindow.open_memory()`；`nebula_page.node_selected` 与 `timeline_page.focus_requested` 均接上 |
| 敏感记忆上下文显隐 | ✅ | `test_sensitive_and_core_memory`：`set_sensitive` → `sensitive_ids()` / `memory_inspect()["sensitive"]`；列表与召回默认 `••••••`，检查器「在当前上下文显示敏感内容」按钮就地揭示 |
| 命名空间预过滤 | ✅ | `test_cognitive_sectors_and_namespaces`：`recall(namespaces=("不存在",)).hits == []`；过滤在 `prefrontal.recall` 打分前生效，不依赖后置过滤 |
| 认知分区可视化（五类） | ✅ | `cognitive_sectors()` 返回 5 类且计数≥1；数据页「认知分区」柱图 + 说明 |
| 星云语义化布局 | ✅ | `neurons._semantic_position`：环=分层（core 内圈/archive 外圈）、扇区=记忆类型、角度≈形成时间；纯确定性，跨设备与截图可复现 |
| 星云图例与节点详情 | ✅ | 页脚图例（颜色/环/扇区/角度含义）；悬停节点、连线看摘要；点击节点打开检查器 |
| 可编辑 Core Memory | ✅ | 四块 human/persona/preferences/constraints → KV + 单条 `policy` 事件（`reason=edit_core_memory`），`projections` 重放到同一 bucket |
| `search_text` 退化路径索引化 | ✅ | `test_text_inverted_index_replaces_full_scan`：正文 2-gram 倒排命中/不误命中/删除后失效；`LanceVectorDB.search_text` 在 FTS 缺失时先走倒排，最后才物化全表 |
| 稀疏通道倒排索引 | ✅ | `_SparseIndex`：`(term,id,weight)` postings，查询只扫命中 term；行数不符整表重建，异常退回全扫且结果一致 |
| 星云构图增量 + 落盘缓存 | ✅ | `test_incremental_memory.py` 4 条：命中缓存零 embedding 调用、新增只补算一条、缓存损坏/签名变化重建、倒排与全扫一致 |
| 重活移出主线程 | ✅ | `ui/desktop/async_backend.py`：单线程池 + `QueuedConnection` 回主线程；`refresh_status()` 在后台忙时跳过本轮 |
| GPU 三通道探针 | ✅ 脚本就绪 | `main.py --probe-models --json` 输出 `verdict` / `all_on_gpu` / `embedder.sparse_enabled`；`self_check` 只构造对象，不能替代本探针 |
| 冻结态资源路径 | ✅ | `core/runtime_paths.py`（dev=项目根，frozen=`_MEIPASS`/exe 旁 + 用户数据目录）；`engine/cuda_runtime.py` 预登记 DLL 目录 |
| 便携发行与换机校验 | ✅ 脚本就绪 | `scripts/build_portable.py`（DLL/权重归集 + spec）、`scripts/verify_portable.py`（改目录、无 `.env`、DLL 不在 venv 下复跑探针要求 `GPU_OK`） |
| 全页截图覆盖 | ✅ | `scripts/desktop_screenshots.py` 覆盖星云/记忆/召回/系统/数据/模型/接口/同步/设置/**时间轴**（`01`~`10`），`--real` 用真实库 |

> 算力现状：本机 GTX 1060 / 6GB / 驱动 581.57 已具备 `CUDAExecutionProvider` 与
> `llama_supports_gpu_offload() == True`，三通道权重齐全、`sparse_head.onnx` 已导出。
> 仍待确认一项（只能靠实跑）：便携目录复制到**另一台机器**后探针是否仍 `GPU_OK`。

## 冻结发行包（PyInstaller onedir）✅（2026-11-06）

> 产物 `dist/super-brain/`：9.6GB（含 3.35GB 权重）。流程见 `WORKING_MEMORY.md` §8。
> 全部在本机实测，命令与输出都在 `build/probe_frozen2.json`、`build/selfcheck_frozen.json`、`build/frozen_ui.png`。

| 验收项 | 状态 | 证据 |
|---|---|---|
| 打包可完成、可复现 | ✅ | PyInstaller 6.22.3，`super-brain.spec --noconfirm` → 274s 完成，`COLLECT` 成功 |
| GUI 能起来且渲染正常 | ✅ | `super-brain.exe` 出窗口「超级大脑 v2.1 · Living Memory」，十一页导航齐全（截图 `build/frozen_ui.png`） |
| 冻结态自检 | ✅ | `super-brain-cli.exe --self-check --json` → relational/vector/graph/kv/timeseries/hypergraph/clock 七项全 `ok=true`，冷启动 0.57s |
| 冻结态 GPU 三通道 | ✅ | `--probe-models --json` → `verdict=GPU_OK`、`all_on_gpu=true`、llm `active_gpu_layers=-1`、embedder/reranker 均 `CUDAExecutionProvider`、`sparse_enabled=true`、`cuda_dll_complete=true`（驱动 API 13.0） |
| CUDA DLL 预登记生效 | ✅ | `cuda_dll_dirs` 命中 `_internal`、`_internal/cuda_bin`、`_internal/llama_cpp/lib`、`_internal/onnxruntime/capi` 四处 |
| 数据不写安装目录 | ✅ | 首次启动自动在 `%APPDATA%\super-brain\data` 建六引擎库；配置读 `%APPDATA%\super-brain\config.env` |
| 体积可控 | ✅ | 去重后 9.6GB：删掉与 `onnxruntime/capi`、`llama_cpp/lib` 同名的 25 个平铺 DLL + `providers_tensorrt`，释放 3397MB，探针复跑仍 `GPU_OK` |
| 生成物不入库 | ✅ | `.gitignore` 增 `build/`、`dist/`、`app_entry.py`、`super-brain.spec`（已 `git check-ignore` 验证） |

> 一个隐性坑记在这里：PyInstaller **不会**自动收集 `llama_cpp/lib/*.dll`（那是 ctypes 运行时加载的），
> 漏了不报错、只是静默退回规则抽取。spec 里显式把它们收进 `llama_cpp/lib` 才有 GPU 推理。

## 多 Agent 共享 + 桌面体验（v2.2 收尾）✅（2026-11-06）

> 定位确认：超级大脑是**所有 Agent 共用的外置记忆中枢**，`agent_id` 只写审计。
> 全量回归：`pytest tests -q` → **129 passed**。

| 验收项 | 状态 | 证据 |
|---|---|---|
| 记忆跨 Agent 共享（不按 agent 隔离） | ✅ | `test_memories_are_shared_across_all_agents`：codebuddy 写入 → dsh 不带任何参数即可召回；反向亦通；`namespace` 去重后仍只有一个值 |
| 多进程同时接入同一内核 | ✅ | 三个 stdio 进程经 `mcp_bridge.json` 转发 `/mcp/call`，并发写入 all applied；`health_check` 6/6 引擎 ok |
| 入口排队（中枢逐条裁决） | ✅ | `HTTPAdapter` 单工作线程 FIFO + `queue_depth`；并发不再出现 WinError 10054 |
| 一键复制 MCP 配置 | ✅ | 接口页「复制给 Agent」；实测用该配置 spawn → `initialize` + `health_check` 通过 |
| 配置不含安装绝对路径 | ✅ | command=`cmd.exe`、args=`/c %LOCALAPPDATA%\SuperBrain\bin\super-brain-mcp.cmd`；启动器每次启动按当前安装位置重写 |
| 单一数据目录 + 迁移 | ✅ | `test_storage_root_switch_migrates_data_and_restarts`：换目录后 `storage_root()` 变更、六引擎数据复制到位、重启后仍能读到迁移前的记忆 |
| HTTP 随软件自启 | ✅ | 启动 16s 内 `mcp_bridge.json` 自动出现、8766 监听；无需手动点「启动 HTTP 服务」 |
| 托盘驻留 / 单实例 / 自启 | ✅ | 二次启动不新增进程且唤回已有窗口；`QTrayIconMessageWindow` 存在；`autostart.py` 写 HKCU Run（`--tray`） |
| 首页资源仪表 | ✅ | 截图 `data/screenshots/01-nebula.png`：CPU 34% / 内存 11.2-15.8GB / 显卡 2% · 0-6144MB（nvidia-smi） |
| 模型运行总览 | ✅ | 截图 `data/screenshots/04-model.png`：三通道 × 本地/远程 × 设备 × 状态 × 模型名 + 总览行 |
| 窗口适配笔记本屏幕 | ✅ | 1536×824 可用区下窗口 `1414×782 @ (61,40)`；接口页加滚动区后主窗口最小高度从 937 降到 656 |

## 明确未做（不在本次七阶段任务内）

- `mcp_server.serve()` 的官方 SDK Streamable HTTP 传输装配（工具面已就绪，需 `pip install mcp` 后接线）。
- 换机实测：发行包复制到另一台机器（有卡 / 无卡各一次）后的 `verdict` 与降级行为。
- 真实 GitHub 私库 pull/push 联调（需 PAT 与 `SYNC_REPO_URL`）。
- 合规硬清除（tombstone purge）：遗忘只做到「移出推理路径 + 保留审计」，物理清除需另建跨六引擎的清除流。
- 敏感记忆加密存储（当前只是前端上下文遮蔽 + 标记位，正文仍明文入库）。
- 多用户 / 多租户权限治理、3D 大脑面板、PAT 自动轮换（v2 路线图；PySide6 桌面前端已替代主要可视化入口）。
