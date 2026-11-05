# 工作记忆 · 当前状态

> 最后更新：2026-11-05 ｜ 当前阶段：**七阶段 + 桌面控制台 + 配置中心（免重编译）全部落地，93 用例全过**

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
| PySide6 桌面控制台 | ✅ | `ui/desktop/`：九页（星云/记忆/召回/系统/数据/接口端口/同步/设置/关于）；22 条用例 |
| 配置中心（免重编译） | ✅ | 设置页预设×3 + 全量参数自动表单；存储引擎开关；local/cloud/auto 模型；内核热重启；10 条用例 |

启动：`python -m ui.desktop`（可交互）；`python scripts/nebula_preview.py`（整窗离屏出图到 `data/nebula_preview.png`）。
端口：接口与端口页可改 host/port 并一键启停 HTTP 适配器；偏好存用户级 QSettings。
未做（明确边界）：官方 MCP SDK 传输装配、NiceGUI 图形页、真实 GPU/模型权重、真实 GitHub 私库联调。
详见 `.memory/ACCEPTANCE.md` 末尾。

## 2. 环境事实（已实测，别重复摸索）

- 解释器：项目内 `.venv`（Python 3.11.5）。系统 PATH 上的 `python` 是 KiCad 自带的，**只有 pydantic/numpy，缺全部存储依赖**，不要用它跑测试。
- 已装：`duckdb 2.0.0` / `lancedb 0.41.0` / `kuzu 0.11.3` / `lmdb 3.0.0` / `hypernetx 2.4.3` / `pydantic 2.14.2` / `PySide6-Essentials 6.12.0` / `keyring 25.7.0` / `pytest 9.2.0`。完整锁定见 `requirements.txt`（16 包）。
- 未装（按降级实现）：`onnxruntime`、`llama-cpp-python`、`fastapi`、`mcp`、`nicegui`、`apscheduler`。
- Qt 测试/离屏渲染必须先设 `QT_QPA_PLATFORM=offscreen`（`tests/test_desktop.py` 模块顶部已设）。
- Qt offscreen 环境里**不要 spawn 子进程**（句柄继承 WinError 50）；`SettingsStore.test_connection()` 因此只探测 `shutil.which("git")`。
- Qt offscreen 平台字体库为空：预览脚本必须 `QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")`，否则中文全是方块。
- 用户配置：`%APPDATA%/super-brain/config.json` 存与出厂默认的**差异补丁**（Path 序列化为字符串，apply 时还原）；测试用 `SUPER_BRAIN_CONFIG_DIR` 重定向；密钥（GitHub PAT / 云端 key）只进钥匙串，绝不进该文件。
- 存储引擎开关语义：relational 恒开（唯一事实源）；关闭的引擎是空实现占位（写 no-op、读空），事件仍在 journal，重开后重放/快照重建 —— 不要给关闭引擎抛异常。
- shell 是 **PowerShell**：别用 `cd /d`；调脚本用绝对路径；命令行里 `$var` 会被 PowerShell 展开（探针会静默吃掉 Cypher 参数）。

## 3. 跑测试的正确姿势（沙箱限制，必看）

本机有 safe-delete 钩子拦截 `shutil.rmtree`，**pytest 默认 basetemp 会触发 `SystemExit: 1` 把所有用例变成 ERROR**（不是代码 bug）：

```powershell
Set-Location -LiteralPath 'B:\Desktop\BaiduSyncdisk\desktop\VSProject\my_brain_backend'
& '.\.venv\Scripts\python.exe' -m pytest tests -q --basetemp="$env:TEMP\pb_$(Get-Random)"
```

- `--basetemp` 必须指向**当次不存在的新目录**（复用同一路径会在 session 开始时删旧目录而崩）。
- 不要把 basetemp 放在项目内（同属被保护目录）。
- 自检：`& '.\.venv\Scripts\python.exe' main.py --self-check`（实测冷启动 0.5~0.9s）。

## 4. 引擎 API 已核实（0.41/0.11 与旧文档不同，照此写）

### lancedb 0.41
- `db.list_tables()`（`table_names()` 已废弃），返回 TableListing，用 `getattr(listing,"tables",listing)` 兼容。
- 稠密列必须 `pa.list_(pa.float32(), dim)`（fixed_size_list），维度不符在 add 时报错 → 已用作维度校验。
- 检索：`tbl.search(vec).metric("cosine").where("is_active = true", prefilter=True).limit(k).to_list()`。
- 全文：`tbl.create_index("text", config=lancedb.index.FTS(), replace=True)`，检索 `tbl.search(q, query_type="fts")`。
- **没有"无查询的 where 扫描"**：`tbl.search()` 不接受空查询 → 稀疏通道要先 `to_arrow()` 全量取行再内存打分。

### kuzu 0.11
- 一个进程一个 `kuzu.Database`；`HypergraphDB` 通过 `KuzuGraphDB.database` 复用，禁止另开。
- 参数 `$name` 字典传入；`MERGE ... ON CREATE SET ... ON MATCH SET` 实测幂等。
- `get_as_df()` 依赖 pandas（未装）→ 统一 `get_column_names()` + `has_next()/get_next()`。

### duckdb 2.0
- 单写多读：同进程多连接写同一文件冲突 → 单连接 + `conn.cursor()` 每线程隔离 + 写锁。

## 5. 已确立的关键设计约定（改动前先读 .memory/DECISIONS.md）

- 记忆自身的 **PAD 不做基线平滑**（保留事件原始色彩）；平滑只用于 Agent 情绪基线（ADR-007）。
- 存储层契约是 `Mapping`，实体只活在 `models/`、`brain/`（ADR-002）。
- 投影失败不回滚事实：SQLite 先落库，outbox 留待重试；embedder 缺失时向量投影标 skipped（可从主库重建）。
- 归档（tier=archive）不参与常规召回（已有用例锁定）。
- 冷静期只影响召回权重与注入语气，**不改写已存记忆的 PAD**。

## 6. 下一步（可选收尾，不在七阶段任务内）

1. `pip install mcp` 后完成 `mcp_server.serve()` 的 Streamable HTTP 传输装配（工具面已就绪）。
2. 配置 `SYNC_REPO_URL` + PAT，联调 `git_worker` 真实 pull/push（离线路径已测）。
3. 桌面前端已完成（`python -m ui.desktop`）；`pip install nicegui` 后可选启用 Web 看板（JSON 兜底已可用）。
4. 放入真实 ONNX/GGUF 权重，跑一次 `eval.compare()` 出换模型对比报告。
5. 生产前必须设置 `GW_MCP_TOKEN_SECRET`，否则令牌进程重启即失效。

## 7. 已知坑

- `sqlite3` 的 FTS5 `unicode61` 对中文两字查询命中 0 条 → 已加 LIKE 兜底（查的是 fts 表的 `body` 列，不是主表不存在的 `content` 列）。
- `sorted()` 混排中文/英文时 ASCII 在前，测试里比较集合别比较列表。
- 不要给 `.gitignore` 加 `.memory`；该目录必须提交。
