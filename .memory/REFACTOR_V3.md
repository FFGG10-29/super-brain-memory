# REFACTOR_V3 —— 最终改造方案（已确认定稿并落地，2026-11-06）

## 0. 实现状态（实机验证通过）

| 项 | 落地位置 | 实测 |
|---|---|---|
| Honker Windows POC | `scripts/poc_honker.py` | 0.5.0 可加载、事务 notify、`listen` 自带兜底轮询、Queue 内建租约 |
| journal group commit + fsync | `v3/journal.py` | 20 并发事件全部 fsync 后返回；`reconcile` 补齐 17 条崩溃缺口 |
| inbox 租约状态机 | `v3/inbox.py` | 双 Worker 无重复 claim；lease 回收；CAS 防双花；event_id 幂等 |
| 闹钟（可退化） | `v3/notify.py` | 本地 Event + UDP 定向唤醒 + 1s 兜底轮询；Honker 默认关（首次事务初始化拖慢在线路径 5.5s） |
| Daemon（纯受理/只读/WS） | `v3/daemon.py`、`v3/daemon_main.py` | HTTP 202、只读 SQLite 投影、WS 广播、进程内不加载模型/六引擎 |
| Worker（认知全链路后台化） | `v3/worker.py`、`v3/worker_main.py` | drain、legacy 迁移保真、CAS 完成、广播 node_created |
| 进程监管 | `v3/supervisor.py` | Job Object + 退避重启 + 熔断；`--separate` 模式实机 `done:1 / memories:1` |
| GPU 星云 | `v3/web/index.html` + `nebula.js` | 纯 WebGL 点精灵 + 加色 bloom + 三层视差星尘 + 语义星团 + 前端脉冲插值 |
| 安装器 | `scripts/installer/*` | WinSW 服务 XML + Inno Setup `setup.iss` + `build.ps1`（ISCC/WinSW 二进制需外部下载后编译） |
| 数据迁移 | `scripts/migrate_v2_to_v3.py` | 旧库只读、含封存记录、保留双时态/PAD；**待显卡机 3000 条数据到位后运行** |
| 验收 | `tests/test_v3_pipeline.py`、`scripts/verify_v3.py` | 全量 139 passed；实机 ack 54–97ms、WS 3 事件、inbox done=3、投影 3 条 |

## 0.2 按 astrbot_plugin_livingmemory 源码重写（2026-11-06）

**源码依据**（`data/_research/livingmemory`，`gh repo clone lxfight/astrbot_plugin_livingmemory`）：
- `package.json`：`three@0.186.1` + `d3-force-3d@3.0.6` + `esbuild`；
- `pages/dashboard/graph-3d-core.js`：社区球壳 `goldenAngle/cbrt` (L90-99)、每星系旋转矩阵 `frames` (L150-155)、
  螺旋臂 `angle = arm*2π/arms + 5.4*r` (L173)、力导向迭代 `max(60,150-n/30)` (L187)、
  每社区边预算 `min(120,max(12,sqrt(size)*5))` 且 degree<4 (L207-215)；
- `pages/dashboard/graph-3d.js`：`THREE.Points + ShaderMaterial + AdditiveBlending` (L244-271) 一次 draw call；
  `galaxyDust = min(24000, nodes*12)` 粒子、每粒子 `aCenter/aSpin` 绕簇心旋转 (L290-342)；
  `setPixelRatio(min(dpr,1.5))` (L43)、Web Worker 跑布局 (L82)、`LineSegments` 共享 position buffer (L277-286)。

**Flutter 等价实现**（`flutter_app/lib/`）：
- `graph_model.dart`：内容 2-gram 贪心社区检测（容量上限强制分散）→ 球壳簇心 + 螺旋臂 + 每簇 frame 矩阵
  + galaxyDust 粒子云 + 边预算截断；纯 TypedArray 式预计算，布局只跑一次；
- `nebula_painter.dart`：`Canvas.drawPoints` 批量绘制（星尘 / 24000 尘埃 / haze / 边 / 星爆 / 节点 / 脉冲 ≈ 7 次 draw call），
  **无逐节点 blur、无 MaskFilter**；`BlendMode.plus` 等价 AdditiveBlending；透视 + yaw/pitch 拖拽翻滚；
  检索命中触发「焦点→命中节点」束状金线星爆（复刻截图 3）；
- `v3/rpc.py` + `v3/daemon.py`：**WebSocket 双向 RPC**（`rpc` / `rpc_result`），
  覆盖十页全部方法（graph/memories/recall/detail/spaces/snapshot/engines/tasks/run_task/
  model_status/config.get/config.set/sync_once/snapshots/create_snapshot/export/gateway/restart）；
- `app_shell.dart`：十页导航 + 顶栏（空间/搜索/导出/全屏/复位）+ 底部状态栏，与 Qt 控制台功能对齐。

**性能实测**：`flutter test` 3000 节点 / 4000 边 → 布局 **163ms**、10 个星系团、24000 尘埃粒子、
边预算截断到 602 条；`flutter analyze` 0 issues、`flutter build windows --release` 通过；
`scripts/verify_v3_rpc.py` 13 个 RPC 方法实机全通。

## 0.1 Flutter 3D 客户端（2026-11-06 已落地并编译通过）

`flutter_app/`：**纯 Dart CustomPainter 实现真 3D 星云**（旋转矩阵 yaw/pitch + 透视投影 + 深度排序 + 加色 bloom），
鼠标拖拽翻滚 / 滚轮缩放 / 双击复位；语义聚类成**多个星系盘**（连通分量种子 + 中文 2-gram 贪心归簇），
簇心球壳分散、簇内螺旋臂 + 薄盘厚度 —— 解决「全部挤一坨」。
零第三方依赖（只用 `dart:io` + Flutter SDK），已用本机 Flutter 3.47.7 `flutter analyze` 0 error、
`flutter build windows --release` 编译出 `super_brain.exe`。
页面：星云 / 记忆管理 / 召回测试 / 接口与端口 / 设置 / 关于；HTTP + WS 直连 v3 Daemon，
后端新增 `GET /v1/recall`、`GET/POST /v1/config`（3D 偏好跨设备同步）。
构建脚本 `flutter_app/scripts/build_windows.ps1`（平台 runner 由本机 SDK 生成，不覆盖 lib/）。

**已知取舍**：安装器脚本齐备但 ISCC/WinSW 二进制需下载；`<50ms` 在 Windows fsync 抖动下实测 54–97ms
（对比 v2 秒级），group commit 已启用，指标按"不等待任何模型"验收。

> 目标：把「单进程里跑完 UI + 六引擎 + 三模型 + 同步写全链路」拆成
> **常驻 Daemon + 无状态 Worker + GPU 前端**。在线只追加，理解全后台，能力一个不砍只换位置。
> 三条铁律：Daemon 请求路径无模型/向量/图谱/去重/合并；前端与 Python 只有 WS/HTTP；
> 可靠性只认 journal + inbox 状态机，组件全是可换件。

## 1. 架构

```
Flutter 壳(设置/表单/列表/托盘) + WebView2·Three.js 星云(GPU)
   │ WS：全量快照 + event_id 增量 + 脉冲事件(前端插值)，JS 直连不经 Flutter 中转
   ▼
Daemon (WinSW/NSSM Windows 服务，SCM 管崩溃重启)
  HTTP · MCP(HTTP+stdio) · WS · /v1/events   ← 所有入口唯一收口
  只做：校验 / event_id 幂等 / 限流 / journal fsync / inbox / 200 accepted
  内管 Worker：subprocess + 退避重启 + 熔断 + Job Object(防孤儿占锁)
   │ LISTEN(闹钟)              │
   ▼                          ▼
Model Worker(单实例+批处理)   brain.db(WAL)          NDJSON journal
 claim(lease/CAS/幂等) →      inbox / 投影表          唯一真源 + HLC
 LLM抽取·SimHash·显著性·       (synchronous=NORMAL)   GitSyncWorker 多端
 语义合并·PAD·FSRS·六库投影     Honker(可选加速)       mark_event_applied
 → CAS done → 广播事件
```

## 2. 组件定稿

| 层 | 定稿 | 否决 |
|---|---|---|
| 监管 | WinSW/NSSM 服务（SCM 失败重启）+ Daemon 内管 Worker + Job Object | Circus（Windows 主线不支持，PR #1224 未合）；独立 Watchdog（谁看护它） |
| 队列/通知 | inbox 表是唯一真队列；Honker NOTIFY 只当闹钟；失败退化命名管道 + 1s 兜底扫描 | 裸 pub/sub 当可靠队列；"一通知一任务" |
| 唤醒三保险 | ① 醒来 drain 到空 ② 启动/重连扫 pending ③ 1–5s 低频兜底 | 只靠通知 |
| 状态机 | 显式 BEGIN IMMEDIATE → 批量 claim(20)+lease → RETURNING → COMMIT；CAS 完成；lease 超时由任意 Worker 原子回收；幂等键=全局 event_id(HLC/UUID7) | 裸 SELECT pending、多语句一次 execute、本地自增 id 当幂等键 |
| 崩溃闭环 | journal fsync → inbox INSERT+NOTIFY → COMMIT → 200；启动从 last_applied 补缺口 | 先 SQLite 后 journal（丢事件） |
| 渲染 | Flutter 只做外框，星云区 WebView2 + Three.js（星团/视差/Bloom/拖尾） | Flutter 硬写 3D；Qt CPU 光栅 |
| 分发 | MSIX/Inno Setup：服务自启、%LOCALAPPDATA% 数据目录、权重首次下载、卸载不丢配置 | 绿色目录；PyInstaller 单 exe |
| 保留能力 | 事件溯源、双时态、PAD、FSRS、语义合并、聚类摘要、六引擎、Git 同步，全部搬进 Worker | 为性能砍差异化 |

## 3. 写路径（定稿，含 fsync 讨论结论）

```
1 校验 / event_id 幂等 / 限流                          <1ms
2 journal.append(event) + fsync（strict；group commit 合批） 1–10ms / P99 30ms
3 BEGIN → INSERT inbox(pending) + NOTIFY → COMMIT         <1ms（WAL NORMAL，不额外刷盘）
4 200 {"accepted": true, "event_id": "..."}                P99 < 50ms
──────────── 请求结束，以下后台 ────────────
5 Worker drain claim → LLM抽取 → SimHash → 显著性 → 语义合并 → PAD → 六库投影 → 冲突消解
6 CAS done → 广播 node_created / superseded / rejected / pulse → 前端本地插值播放
```

- **D1** 在线不再写 SQLite neuron 表：journal 升为唯一真源，主库/六库全是可重建投影。
- **D2** 在线只强刷一次盘：journal strict fsync；inbox 弱持久化靠 journal gap 补齐兜底。
- **D3** group commit；指标 P99<50ms，不写 <10ms 硬 SLA；耐久档 strict(默认)/fast。
- **D4** API 语义 = accepted 而非 stored：写完立刻查不到，靠 WS 事件更新；对标腾讯 TencentDB Agent Memory（L0 毫秒落盘→后台 6s 提炼→10s 内可检索）。
- 所有入口（MCP over HTTP、stdio、外置共享大脑、托盘）统一收口 `/v1/events`，禁止直调 `hippocampus.remember`。

## 4. 阶段与验收

| 阶段 | 产出 | 硬指标 |
|---|---|---|
| POC 1 | Honker Windows 加载 / 事务内 NOTIFY / 跨进程唤醒 / Streams | 五件事全绿；挂则退化命名管道 |
| POC 2 | inbox 状态机：并发 claim、kill -9、lease 回收、CAS、幂等 | 不丢不重 |
| 0 | Qt 不动，outbox 改后台消费 + 准入门控搬后台 | 写入 P95 秒级→毫秒级；UI 不假死 |
| 1 | Daemon 服务化 + Worker 独立进程 + WS 收口 | 10 客户端并发无 database is locked；Worker 崩租约内重做 |
| 2 | Flutter 壳 + WebView2/Three.js 星云 | 3000 节点 60fps；断线重连恢复 |
| 3 | 安装器 / 集中日志 / 密钥由 Daemon 下发 / 升级迁移 | 选路径安装→重启自启→卸载不丢配置 |
| 4 | GPU 批处理 / inference server / 多设备实测 | 显卡机 CUDA 全链路 |

## 5. 风险回退

Honker 挂不上→命名管道+兜底扫描；WebView2 缺失→安装器引导/全 Web 壳；fsync 长尾→group commit 不放弃 strict；多 Worker 显存×N→单 Worker+批处理；断电双写窗口→journal 游标补齐；权重体积→首次下载。

## 6. 与现有代码的关系

不是重写，是改调用时机 + 补进程边界：outbox=inbox 雏形、journal+HLC+mark_event_applied=现成幂等真源、六引擎投影器=Worker 任务体、语义合并/PAD/FSRS=后台逻辑、MCPToolkit=Daemon API 面、Dashboard 快照=WS 首屏数据。阶段 0 可先用现有 Qt 前端量收益。

## 7. 存量数据迁移（**暂缓**：3000+ 条在另一台电脑上，本机只有 26 条演示数据）

等显卡机数据到位后作为 **POC 0** 执行，先于阶段 0：
1. 关闭桌面实例（Kùzu 单进程锁）；旧 `data/` 只读，**绝不原地改**，新目录保持干净。
2. 只读打开旧 SQLite（实际表名复数：`neurons / synapses / hyperedges / outbox / applied_events`；正文在 `payload` JSON 列，双时态/PAD/tier 在独立列）。
3. 全表扫描**含 is_active=0 的封存记录**，按 tx_from 排序生成 ingest 事件，event_id=原 uuid7 id，保留 valid_from_ms/tx_from/pad/tier/pyramid，禁止重打时间戳。
4. 若 `brain-sync/` 已有 NDJSON 事件段，先按 HLC 合并去重（mark_event_applied），SQLite 补漏。
5. 追加新 journal（合批 fsync）→ Worker 重放 → 六库投影重建；**派生库不搬文件，只重放**（跨机器/跨版本 Kùzu、LanceDB 直接拷不可靠）。
6. 校验：新旧 count(neurons) 一致、封存数一致、FTS 抽样召回一致、PAD 分布一致；旧目录保留 N 天可回退。
前端不暴露"指向旧文件"，改为安装器/首次启动检测旧目录后一键回填。

## 8. 下一步

从 **POC 1（Honker Windows 加载）** 开工——唯一可能推翻选型的未知数；POC 期间不动现有代码。
