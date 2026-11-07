# Super Brain · Flutter 客户端

纯 Dart / Flutter Windows 桌面端，零第三方依赖（只用 `flutter` SDK + `flutter_test`）。
它**只是 UI**：所有记忆读写都经 WebSocket RPC 交给独立的 Python 无头后端进程。

## 进程分离

```
Flutter 进程                          Python 无头后端（单进程，唯一持有六引擎）
┌──────────────────────┐            ┌────────────────────────────────────────┐
│ 14 个功能页           │  WS RPC   │ build_app() 六引擎（Kùzu 唯一写者）      │
│ BackendLauncher 探活  │ ────────▶ │ 8767 /ws   RPC 分发 + node_created 广播  │
│ → 未启动则拉起后端     │  8767     │ 8768 /v1/* HTTP + WebGL 星云静态页       │
│ BrainClient（HTTP兜底）│ ────────▶ │ 8766 /mcp  标准 MCP（所有 Agent 接入）   │
└──────────────────────┘  8768     └────────────────────────────────────────┘
```

启动时 `main.dart` 调 `BackendLauncher.ensureRunning()`：先探活 `GET /v1/health`，
没起来就 `Process.start(..., mode: detached)` 拉起 `python -m headless_server` 并轮询 30s；
后端作为独立进程常驻，Flutter 退出不影响它。启动命令/目录/参数可在「全量配置」页改，
存 `%APPDATA%/super-brain/flutter_prefs.json`。

**为什么后端必须单进程**：Kùzu 一个库只允许一个写者；MCP 网关与 v3 Worker 共用
同一份 `build_app()` 实例，v3 的只读投影取 `config.storage.sqlite_path`，与 v2 主库天然同源。

## 启动

```powershell
# 开发态（Flutter 会自动拉起后端，无需手动开服务）
powershell -ExecutionPolicy Bypass -File flutter_app\scripts\run_windows.ps1

# 只想手动起后端
.\.venv\Scripts\python.exe -m headless_server
# 可选：--http-port 8768 --ws-port 8767 --mcp-port 8766 --fast --no-circadian

# 端到端验收（临时目录隔离，18+ 项断言：MCP 六工具 / 写入 / 召回 / 导入 / 换核）
.\.venv\Scripts\python.exe scripts\verify_headless.py
```

> 首次运行需要 Flutter SDK（本机当前未安装，故 `flutter analyze` / 编译产物尚未验收）。
> 若 `flutter pub get` 报 SDK 约束不匹配，删掉 `pubspec.lock` 后重新生成即可。

## 14 个页面

| 页面 | 能力 |
|---|---|
| 星云视图 | 纯 Dart 3D 星系（球壳 / 螺旋臂 / 星尘），拖拽 yaw/pitch、滚轮缩放、检索星爆、WS 光脉冲 |
| 记忆管理 | 写入记忆（异步受理）、列表、`node_created` 自动刷新 |
| 召回测试 | 真实六通道召回（非降级 2-gram），命中打分 |
| 系统概览 | 六引擎健康、昼夜任务手动执行、快照统计 |
| 展示数据 | 结构化快照 JSON、导出快照 + 记忆 |
| 模型配置 | 三通道状态、API 地址、内核重启 |
| 接口与端口 | HTTP/WS 地址、inbox/worker/journal 计数、MCP 工具列表 |
| 同步与备份 | 立即同步、创建快照、快照列表 |
| 设置 | 主机/端口、3D 参数（焦距/星点/Bloom/自转）、重连 |
| **记忆检查器** | 来源/双时态/置信度分解/关联突触/审计轨迹；纠正、遗忘封存、敏感标记 |
| **事件时间轴** | journal 全序 + 缓冲层事件 + 召回日志，类型筛选、3s 自动刷新 |
| **批量导入** | txt/md/json/ndjson/jsonl/csv，快速模式（跳过 LLM 抽取） |
| **全量配置** | 后端 BrainConfig 全参数动态表单、预设、恢复出厂、Core Memory、MCP 配置复制、后端进程设置 |
| 关于 | 架构说明 |

## 与 Qt 前端的关系

Qt 前端 `ui/desktop/` 目前**仍保留**作为功能兜底与对照。按 ADR-021 的约定，
需先在有 Flutter SDK 的机器上完成 `flutter analyze` + `flutter build windows` + 逐页验收，
确认功能等价后再删除 `ui/desktop/` 并改造打包入口。后端已不依赖任何 Qt 模块
（`core/user_config.py`、`gateway/bridge_launcher.py`、`brain/memory_service.py` 已下沉）。
