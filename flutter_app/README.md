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

## 18 个页面（完全覆盖 Qt 11 页，并补齐全部 6 项差异）

| 页面 | 能力 |
|---|---|
| 星云视图 | 纯 Dart 3D 星系（球壳 / 螺旋臂 / 星尘），拖拽 yaw/pitch、滚轮缩放、检索星爆、WS 光脉冲 |
| 记忆管理 | 写入记忆（异步受理）、列表、`node_created` 自动刷新 |
| 召回测试 | 真实六通道召回 + **检索即解释**：通道分解（召回→注入）、单条归因（余弦/权重/PAD）、注入上下文复制 |
| **硬件仪表** | 顶栏同款 CPU / 内存 / 显卡实时环形仪表的大表 + 明细（显存、温度、采样时间） |
| **PAD 认知分区** | P–A 情绪散点（点色=类型、点径=置信度）+ 按类型/分层的 P/A/D 均值，点选联动检查器 |
| **2D 语义环** | Qt 语义环布局的服务端等价投影：环=分层 / 扇区=类型 / 角度=形成时间；节点标签、关系连线、点击联动检查器与 3D 星云 |
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
| **桌面能力** | 托盘驻留 / 单实例 / 开机自启 / 系统钥匙串（DPAPI 兜底）/ 数据目录一键迁移 |
| 关于 | 架构说明 |

顶栏右侧常驻 **CPU / 内存 / 显卡环形仪表**（后端 `system.stats` 每 2 秒采样：psutil + `nvidia-smi`，
N 卡缺失时显示「—」），与 Qt 顶栏仪表等价。

## 发行包：后端随包部署

**不能把 Python 塞进 `super_brain.exe` 内部**——Flutter 编译出来的是原生 Dart 二进制，
里面没有 Python 解释器。可行的做法是「一个安装包、两个进程」：安装时把冻结好的后端
一起部署到 `backend\`，Flutter 启动时自动拉起它。目标机器**不需要装 Python、PySide6
或任何依赖**。

```
{app}\
  super_brain.exe              ← 快捷方式只指向它（双击即启动）
  flutter_windows.dll, data\
  backend\
    headless-server.exe        ← 冻结后端（无 GUI、无 Qt）
    headless-server-cli.exe    ← 排障用（--self-check / --probe-models）
    _internal\
```

`BackendLauncher.resolveDefaults()` 的优先级：
① 用户显式配置 → ② 安装目录 `backend/headless-server.exe` → ③ 开发态
`.venv\Scripts\python.exe -m headless_server` → ④ PATH 上的 python。
后端 exe 分支的启动参数必须为空（旧偏好里的 `-m headless_server` 会被自动忽略）。

一键构建（需要 Python venv + Inno Setup；没有 Flutter SDK 就加 `-InstallSdk`）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_full_release.ps1 -FlutterSdk 'D:\flutter' -InstallSdk -Launch
```

分步：

```powershell
.\.venv\Scripts\python.exe scripts\build_portable.py --headless
.\.venv\Scripts\python.exe -m PyInstaller super-brain-backend.spec --noconfirm
.\.venv\Scripts\python.exe scripts\build_portable.py --trim dist\super-brain-backend
powershell -ExecutionPolicy Bypass -File flutter_app\scripts\build_windows.ps1
powershell -ExecutionPolicy Bypass -File scripts\installer\build.ps1
ISCC.exe scripts\installer\setup.iss
```

> Flutter SDK 已装到 `D:/flutter`（3.47.9 / Dart 3.13.7），VS BuildTools 的 MSVC 也已装齐：
> `flutter pub get` / `flutter analyze`（**No issues found**）/ `flutter build windows --release` 全部通过。
> 构建命令必须带 **`--no-tree-shake-icons`**，否则只在 const Map 里动态引用的图标字形会被裁掉
> （现象：左栏「记忆洞察」「Agent 上下文」没有图标）。`scripts/build_windows.ps1` 已内置该参数。
> 后端起来后也可以直接看同源数据的 WebGL 星云页：`http://127.0.0.1:8768/`。

## 进程模型（澄清「谁是主进程」）

| 形态 | 主进程 | UI 进程 | 说明 |
|---|---|---|---|
| 旧 v2.2 | **PySide6 桌面进程**（`super-brain.exe`） | 同一个进程 | UI 同时持有 `build_app()` 六引擎、8766 网关、托盘、自启；没有独立后端 |
| 现在 v3.1 | **无头后端 `headless_server`** | **Flutter `super_brain.exe`**（另一进程） | 后端唯一持有六引擎；Flutter 只是 WS 客户端，并负责探活/拉起后端 |
| 可选调试入口 | Qt 桌面进程 | 自身 | `python -m ui.desktop` 显式调用才启动；与后端互斥（Kùzu 单写者） |

Qt 代码约 **7,000 行 / 630 KB 源码**，但真正占体积的是 `PySide6-Essentials`（200–300 MB）。
默认发行包用 `--headless` 模板把它 `excludes` 掉，Qt 源码不进包，运行时也完全不加载；
只有传 `-IncludeDebugConsole` 才会额外部署一份源码 + Python 运行时供排障。

## 与 Qt 前端的关系

Qt 前端 `ui/desktop/` 目前**仍保留**作为隐藏调试入口与功能对照。按 ADR-021 的约定，
需先完成 `flutter build windows` + 逐页验收，确认运行时等价后再删除 `ui/desktop/`。
后端已不依赖任何 Qt 模块（`core/user_config.py`、`gateway/bridge_launcher.py`、
`brain/memory_service.py` 已下沉）。

### 2026-11-08 补齐：Qt 独占能力全部下沉到 `core.*`

原先 ADR-021 列的 6 项差异已逐项实现，Flutter 侧**不再有功能缺口**：

| 原差异 | 后端 | Flutter |
|---|---|---|
| 托盘 / 单实例 / 自启 | `core/win32_tray.py`（纯 ctypes Win32 托盘 + 消息循环）、`core/desktop_features.py`（winreg Run 键） | `win32.dart`（本地端口单实例锁 + user32 FFI 显示/隐藏窗口）、`windows/runner/win32_window.cpp` 拦截 `WM_CLOSE` 为隐藏、`桌面能力` 页开关 |
| 钥匙串 | `core/desktop_features.py::SecretStore`（keyring 优先，DPAPI 加密文件兜底） | `桌面能力` 页三账号 PAT/API Key，只显示「是否已保存」，不回显明文 |
| 数据目录一键迁移 | `core/desktop_features.py::migrate_data` + `core/runtime_paths.py::data_dir()` 读 `data_pointer.json` | `桌面能力` 页：查看当前目录/文件数/大小 → 复制到目标 → 重启后端生效（默认保留原目录） |
| PAD 认知分区图表 | `v3/rpc_ext.py::_pad_distribution`（主库真实 pad_p/a/d） | `analytics.dart::PadAnalyticsPage`（散点 + 分类型/分层均值） |
| 召回归因可视化 | `brain/memory_service.py::recall` 已有 `audit`（counts/injected/weights/similarities）与 `injection` | `analytics.dart::RecallAnalyticsPage` 三块解释面板 + 复制注入上下文 |
| 2D 检查器联动 | `v3/rpc_ext.py::_graph_projection`（Qt `_semantic_position` 的服务端等价，坐标确定性） | `analytics.dart::Graph2DPage`，点节点 → 跳记忆检查器（带 `initialId`）或回 3D 星云发检索光脉冲 |
| CPU/内存/显卡仪表 | `core/system_stats.py`（psutil + nvidia-smi，2s 守护线程） | `system_gauges.dart` 顶栏环形仪表 + `硬件仪表` 大表页 |

**零第三方 pub 依赖**：Win32 句柄用 `dart:ffi` 直接调 `user32.dll` 五个函数，
UTF-16 缓冲区用 `msvcrt.dll` 的 `malloc/free` 手动分配，不引入 `package:ffi`、
`window_manager`、`tray_manager` 等需要 C++ 编译的插件。

冻结包注意：`scripts/build_portable.py` 的 `--headless` spec 已把
`core.system_stats` / `core.desktop_features` / `core.win32_tray` / `psutil` / `winreg`
加进 `hiddenimports`（函数内惰性 import 静态分析会漏）。
