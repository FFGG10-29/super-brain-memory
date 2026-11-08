# Super Brain v3.1.0 · Flutter 前端定稿

> 一个跑在本机的记忆中枢：所有 AI Agent 通过 MCP 读写同一份记忆，数据不出本机。

## 这一版最重要的事

### 1. 3D 星云重做（看得见的记忆）
- **语义星团**：内容 2-gram 聚类成 12 个星团，簇内 66% 双致密核（近高斯）+ 34% 对数螺旋悬臂，跨簇弱连线。
- **按类型着色**：事实=金橙、场景=紫罗兰、代码=青蓝、情绪=紫红、画像=暖金；每个星团一团同色云气。
- **空间归一化**：1261 个节点也完整落在视野内（旧版簇心能飞到 ±800，屏幕只剩零星几粒）。
- 中枢（刚用过的记忆）压暗 30%，黑洞（久未使用）缩到 62% 退到角落，只作景深锚点。
- 连线默认关闭，开启后按正弦波呼吸；LOD 三档，拉远自动降细节。

### 2. 修复两个致命问题
- **首屏 0 节点**（状态栏「记忆 1000 / 节点 0」）：后端比 UI 晚就绪时，WS/HTTP 都失败，
  而空结果被当成「成功空图」且永不重试。现在：`graph()` 重试 3 次 → 页面再重试 10 次 →
  WS 连上自动补拉；错误如实显示，不再伪装成 0 节点。
- **一堆光点看不清**：偏好里 `star_size=8.0`（默认 3.2）把每个点画成 9px 光球。现在钳制在 1.4~4.5，
  结构交给星团云气承担。

### 3. 导航与配置
- 左栏 **7 组主导航**（星云视图 / 记忆工作台 / 记忆洞察 / Agent 上下文 / 运行监控 / 系统设置 / 关于），组内页面用顶栏子标签切换。
- 设置页去重并分三子标签；**全量配置 12 组分类**，常用置顶、高级折叠，每个字段都有中文名 + 悬停说明。
- 记忆管理按类型/分层分类，最新记忆置顶。

### 4. MCP 能力面（53 工具 / 6 resources）
- `gateway/tool_catalog.py` 作为单一真源，MCP / HTTP / stdio 三通道自动同步。
- 6 个 resources：`brain://runtime/memory`、`brain://project/archive`、`brain://spaces`、
  `brain://skills`、`brain://manifest`、`brain://health`。
- 鉴权头是 **`X-Super-Brain-Bridge`**（不能写 `Authorization: Bearer`）。
- `recall_memory` 默认毫秒级投影召回，`deep=true` 才走六通道精排。

## 安装

**这是分卷安装包**（全量包含 3.3GB 模型权重，单文件超过 Windows Setup 的 4.2GB 上限）：

1. 把本 Release 里的 **4 个文件**下载到同一个文件夹：
   `SuperBrainSetup.exe` + `SuperBrainSetup-1.bin`(1.90GB) + `SuperBrainSetup-2.bin`(1.90GB) + `SuperBrainSetup-3.bin`(520MB)
2. 双击 `SuperBrainSetup.exe`；
3. 安装过程中提示「请插入下一张磁盘」时，直接点 **确定**——分卷就在同一目录里。

安装完成后：桌面 `Super Brain` → `super_brain.exe`（Flutter 窗口），它会自动拉起
`backend\headless-server.exe`（冻结的 Python 后端），目标机不需要装 Python / Flutter。

## 校验

```cmd
certutil -hashfile SuperBrainSetup.exe SHA256
```

应与 Release 里的 `SuperBrainSetup.sha256` 一致（逐文件比对，GitHub 也会在资产详情页显示 digest）。

## 已知事项

- 数据默认落 `D:\Super-Brain\Data`，可在「系统设置 → 设置」换盘迁移。
- 首次启动若在低配机（机械盘）上，后端六引擎冷启动约 1~3 秒；星云页会显示
  「等待后端返回记忆节点…」并在就绪后自动出图。
- Qt 桌面端已降级为隐藏调试入口（`python -m ui.desktop`），发行包内不含 Qt。
