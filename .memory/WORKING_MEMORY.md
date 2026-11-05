# 工作记忆 · 当前状态

> 最后更新：2026-11-05 ｜ 当前阶段：**生产就绪评估完成，代码已推送 GitHub 私库（FFGG10-29/super-brain-memory），待显卡机实测**，113 用例全过

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
| PySide6 桌面控制台 | ✅ | `ui/desktop/`：十页导航（星云/记忆/召回/系统/数据/**模型配置**/接口端口/同步/设置/关于） |
| 配置中心（免重编译） | ✅ | 预设（均衡/核显/独显）+ 全量参数自动表单（高级参数折叠）+ 存储引擎开关 + 模型双通道（embed/llm 独立供应商）+ 内核热重启 |
| 语义合并 + 聚类摘要 | ✅ | `brain/memory_consolidator.py`：写入裁决 add/update/merge/skip；夜间聚簇 LLM 主题摘要；`tests/test_semantic_merge.py` |
| MCP stdio 接入 | ✅ | `gateway/mcp_stdio.py`：save/recall/timeline/profile/conflict/health 六工具，外部 Agent（WorkBuddy/CodeBuddy/DSH）可挂载 |
| 记忆批量导入 | ✅ | txt/md/json/ndjson/csv → 完整写入流；导出 ndjson 双向打通 |
| 评估报告 | ✅ | `.memory/EVALUATION.md`（对标 Mem0/Letta/Zep/腾讯，两大短板已补齐）+ `.memory/LIFECYCLE_TRACE.md`（MCP 记忆全链路案例） |

启动：`python -m ui.desktop`；改完代码重启：`powershell -File scripts/restart_frontend.ps1`。
离屏整窗截图：`python scripts/desktop_screenshots.py` → `data/screenshots/`。
演示数据：`python scripts/seed_demo.py`（先关桌面窗口，Kùzu 单进程写）。

## 2. 环境事实（已实测，别重复摸索）

- 解释器：项目内 `.venv`（Python 3.11.5）。系统 PATH 上的 `python` 是 KiCad 自带的，**缺全部依赖**，别用它跑任何东西。
- 已装：`duckdb 2.0.0` / `lancedb 0.41.0` / `kuzu 0.11.3` / `lmdb 3.0.0` / `hypernetx 2.4.3` / `pydantic 2.14.2` / `PySide6-Essentials 6.12.0` / `keyring 25.7.0` / `mcp 1.30.0`（**必须 <2**，2.x 改了 API）/ `pytest 9.2.0`。锁定见 `requirements.txt`。
- 未装（按降级实现）：`onnxruntime`、`llama-cpp-python`、`fastapi`、`nicegui`、`apscheduler`。
- GitHub：gh CLI 已登录账号 **FFGG10-29**；私库 `github.com/FFGG10-29/super-brain-memory`（remote origin 已配）。事件同步库建议另建（代码库与事件总线分开）。
- Qt 测试/离屏渲染必须先设 `QT_QPA_PLATFORM=offscreen`（`tests/test_desktop.py` 模块顶部已设）。
- Qt offscreen 环境里**不要 spawn 子进程**（句柄继承 WinError 50）；字体库为空，截图脚本须注入 `msyh.ttc`。
- 用户配置：`%APPDATA%/super-brain/config.json` 存差异补丁；测试经 conftest 的 `SUPER_BRAIN_CONFIG_DIR` 重定向（曾因未隔离污染真实配置导致前端启动崩溃——已修，别回退）。密钥只进钥匙串。
- 存储引擎开关语义：relational 恒开；关闭=空实现占位，事件可重放重建。
- shell 是 **PowerShell**：别用 `cd /d`；命令行内嵌 Python 代码时避免三引号字符串（PowerShell 转义会碎），改写临时脚本文件执行。

## 3. 跑测试的正确姿势（沙箱限制，必看）

```powershell
Set-Location -LiteralPath 'B:\Desktop\BaiduSyncdisk\desktop\VSProject\my_brain_backend'
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

## 6. 下一步（显卡机实测路线）

1. 显卡机 clone：`git clone https://github.com/FFGG10-29/super-brain-memory` → venv + `pip install -r requirements.txt`。
2. 模型权重放 `data/models/`（BGE-M3 / bge-reranker ONNX、Qwen GGUF）→ 应用「独显」预设 → `--self-check` 验证 CUDA。
3. 设 `GW_MCP_TOKEN_SECRET`；跑一次「快照 → 删库 → 恢复」演练。
4. 事件同步库：可另建 `super-brain-memory-events` 私库，把 `SYNC_REPO_URL` 填进「同步与备份」页（代码库与事件总线分开）。
5. WorkBuddy/CodeBuddy/DSH 的 mcp.json 指向 `python -m gateway.mcp_stdio`（见 `gateway/mcp_stdio.py` docstring）。
6. PyInstaller 单 exe 打包（onnxruntime/llama-cpp/kuzu hidden imports 有坑）放实测之后。

## 7. 已知坑

- `sqlite3` FTS5 `unicode61` 中文整串查询命中 0 → LIKE 兜底查 `body` 列。
- `sorted()` 混排中英文 ASCII 在前，测试比较集合别比较列表。
- 不要给 `.gitignore` 加 `.memory`；该目录必须提交。
- pytest 进程内 `SUPER_BRAIN_CONFIG_DIR` 是**进程级共享临时目录**，测试写入会跨用例可见。
