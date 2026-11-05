# 七阶段验收台账

> 最近一次全量回归（2026-11-05）：**83 passed**（含桌面前端 22 条），`verify_baseline.py` 基线成立，`main.py --self-check --json` 退出码 0。
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

布局（对齐 LivingMemory）：左侧 220px 导航九页 · 顶栏（空间选择/返回全景/全局搜索/导出/管理数据/全屏）·
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

## 明确未做（不在本次七阶段任务内）

- `mcp_server.serve()` 的官方 SDK Streamable HTTP 传输装配（工具面已就绪，需 `pip install mcp` 后接线）。
- 真实 GPU / 真实 ONNX 与 GGUF 模型权重加载（当前全程走确定性降级与规则抽取）。
- 真实 GitHub 私库 pull/push 联调（需 PAT 与 `SYNC_REPO_URL`）。
- NiceGUI Web 图形页面、3D 大脑面板、PAT 自动轮换（v2 路线图；PySide6 桌面前端已替代主要可视化入口）。
