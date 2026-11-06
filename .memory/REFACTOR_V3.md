# REFACTOR_V3 —— 最终改造方案（已确认定稿，2026-11-06）

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
