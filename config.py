"""超级大脑 v2.1 —— 一切参数外置。

所有可调参数集中于此，通过 .env / 环境变量覆盖。认知层与网关层只读取本配置，
不直接硬编码任何阈值、权重、端口或私钥。
"""
from __future__ import annotations

import os
import re
import socket
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.runtime_paths import data_dir, env_file, is_frozen, models_dir, project_root, resource_root

# 开发态=项目根；冻结态=exe 目录。DATA_DIR 在冻结态指向用户可写目录，
# 绝不写进 Program Files 或 _MEI 临时目录（无权限 / 退出即丢）。
PROJECT_ROOT = project_root()
RESOURCE_ROOT = resource_root()
DATA_DIR = data_dir()

# 嵌套配置（ModelConfig / SyncConfig / GatewayConfig ...）各用 MODEL_ / SYNC_ / GW_
# 前缀读环境变量，但 pydantic-settings 的 env_file 只作用于 BrainConfig 自身字段。
# 把 .env 注入 os.environ，这些子模型才能真正读到 .env 里的配置；已有环境变量优先。
load_dotenv(env_file(), override=False)

# 打包发行版不依赖外部 .env：默认就让三通道尝试上 GPU。
# 驱动过旧或 CUDA 运行库缺失时，由 cuda_runtime / provider 解析静默回落 CPU，
# 不会拒绝启动（老机器仍可用，只是慢）。
if is_frozen():
    os.environ.setdefault("MODEL_USE_CUDA", "true")
    os.environ.setdefault("MODEL_LLM_GPU_LAYERS", "-1")


def _default_device_id() -> str:
    """设备标识默认取主机名（同步平面按 device_id 分目录，必须跨设备唯一可辨）。

    只保留安全字符：journal 目录名要进 Git 与文件系统，避免中文/空格带来的路径歧义。
    """
    host = socket.gethostname().strip().lower()
    safe = re.sub(r"[^a-z0-9._-]+", "-", host).strip("-")
    return f"dev-{safe or 'unknown'}"


class StorageConfig(BaseSettings):
    """六引擎物理基座连接参数。"""
    model_config = SettingsConfigDict(env_prefix="STORAGE_")

    sqlite_path: Path = DATA_DIR / "relational.db"      # 元数据主库 + FTS5
    lancedb_path: Path = DATA_DIR / "vector.lance"      # 向量库
    kuzu_path: Path = DATA_DIR / "graph.kuzu"           # 图谱 / 超图编码
    lmdb_path: Path = DATA_DIR / "kv.lmdb"              # KV 画像
    duckdb_path: Path = DATA_DIR / "timeseries.db"      # 时序 OLAP
    sqlite_busy_timeout_ms: int = 5000                  # WAL busy_timeout
    wal_autocheckpoint: int = 1000
    lmdb_map_size_mb: int = 256                         # LMDB 地址空间上限
    lmdb_max_buckets: int = 64                          # 命名子库上限
    duckdb_threads: int = 2                             # 时序分析并行度
    # 引擎开关：False = 用空实现占位（写操作 no-op、读操作空结果）。
    # 事件日志是唯一真源：关掉通道不丢数据，重新打开后可用快照/重放重建。
    # relational 是唯一事实源，强制启用，不提供开关。
    engines_enabled: dict[str, bool] = Field(
        default_factory=lambda: {
            "relational": True, "vector": True, "graph": True,
            "kv": True, "timeseries": True, "hypergraph": True,
        }
    )


class ModelConfig(BaseSettings):
    """算力层三模型。藏于接口之后，可整体换远端 API。"""
    model_config = SettingsConfigDict(env_prefix="MODEL_")

    dense_dim: int = 1024                                # BGE-M3 稠密维度
    embedding_model: str = "BAAI/bge-m3"                 # 稠密+稀疏
    reranker_model: str = "BAAI/bge-reranker-v2-m3"      # 交叉编码精排
    llm_gguf_file: str = "qwen3-0.6b-q8_0.gguf"         # 本地 GGUF 文件名（严格 JSON 抽取/总结）
    llm_gguf_dir: Path = models_dir()
    use_cuda: bool = False                               # CUDA→CPU 自动降级
    llm_gpu_layers: int = -1                             # n_gpu_layers 递减降级
    llm_max_tokens: int = 512                            # 单次 JSON 抽取生成上限，防止 thinking 失控
    onnx_providers: list[str] = Field(default_factory=lambda: ["CPUExecutionProvider"])
    # 模型来源：local=全本地；cloud=云端 OpenAI 兼容 API（未配齐自动退本地）；auto=本地有模型文件用本地，否则云
    # 每条通道独立选择本地 / 云端（OpenAI 兼容），地址与 key 各自独立
    # —— Embedding 和 LLM 可以是两家不同供应商（如 OpenAI 向量 + DeepSeek 对话）。
    embed_backend: Literal["local", "cloud"] = "local"
    embed_base_url: str = ""                             # 如 https://api.openai.com/v1（不带尾斜杠）
    embed_model: str = ""                                # 如 text-embedding-3-small
    embed_dim: int = 1024                                # 云端向量维度，必须与 dense_dim 一致
    embed_timeout_sec: int = 30
    embed_api_key_env: str = "EMBED_API_KEY"             # key 来源：钥匙串(embed_api_key) > 该环境变量
    llm_backend: Literal["local", "cloud"] = "local"
    llm_base_url: str = ""                               # 如 https://api.deepseek.com/v1
    llm_model: str = ""                                  # 如 deepseek-chat / gpt-4o-mini
    llm_timeout_sec: int = 30
    llm_api_key_env: str = "LLM_API_KEY"
    # ★ 本地 LLM 总开关（MODEL_LLM_ENABLED，默认 True=启用 GPU/CPU 推理）。
    #   2026-11-10 实测：GTX 1060(Pascal 6.1) 上 bge-m3+reranker(CUDA)+qwen3-0.6b
    #   (n_gpu_layers=-1) 三模型同进程共存、推理全部正常，无需禁用 LLM。
    #   此前误判的 ggml-cuda.cu:109 "CUDA error" 实为进程关闭时 CUDA context 释放的
    #   stderr 噪音（紧跟 WS 断连），非运行期崩溃。本开关仅作为可选降级逃生口：
    #   置 False 时注入 DisabledLLM，裸写走规则抽取（PAD 有非零基线），用于低配/无 GPU 机器。
    llm_enabled: bool = True


class RecallConfig(BaseSettings):
    """检索融合权重与配额，全进 config 靠 eval 调参。"""
    model_config = SettingsConfigDict(env_prefix="RECALL_")

    rrf_k_oversample: int = 4                            # 各路超采样 k×4
    rrf_constant: int = 60                               # 1/(k+rank)
    w_rrf: float = 1.0                                   # RRF 名次
    w_dense: float = 0.35                                # ★ B 包：dense cosine 相似度直接进总分（旧版只以名次进 RRF）
    w_freshness: float = 0.6                             # 新鲜度
    w_synapse: float = 0.8                               # 突触强度
    w_pad: float = 0.5                                   # PAD 显著性
    w_confidence: float = 0.7                            # 置信度
    top_k: int = 5
    p95_target_ms: int = 800                             # CPU 单次召回 P95
    pad_salience_arousal_weight: float = 0.6             # PAD 显著性里唤醒度的占比
    rerank_enabled: bool = True                          # False=跳过交叉编码精排（省时省电，低配设备）
    rerank_top_n: int = 20                               # W5 / C3：精排候选硬上限压到 20
    rerank_timeout_ms: int = 8000                        # W5 / C3：端到端 deep 15s 预算内给精排 8s 硬超时


class PlasticityConfig(BaseSettings):
    """FSRS 三变量遗忘/强化曲线参数（离线再拟合）。"""
    model_config = SettingsConfigDict(env_prefix="PLAST_")

    fsrs_difficulty_default: float = 5.0                 # D 难度
    fsrs_stability_default: float = 1.0                  # S 稳定度
    fsrs_decay: float = 0.9                              # 幂律衰减底数
    reinforce_threshold: int = 3                         # 访问≥N 次触发巩固
    consolidate_confidence: float = 0.95                 # 核心层阈值
    recall_layer_ceiling: float = 0.95
    review_queue_ceiling: float = 0.5                    # <0.5 入复核队列
    fsrs_power: float = 0.9                              # 幂律衰减指数
    fsrs_arousal_boost: float = 0.5                      # 高唤醒对稳定度增长的加成（闪光灯记忆）


class TierConfig(BaseSettings):
    """分层 × 金字塔正交流动。"""
    model_config = SettingsConfigDict(env_prefix="TIER_")

    tiers: list[Literal["buffer", "core", "recall", "archive"]] = \
        Field(default_factory=lambda: ["buffer", "core", "recall", "archive"])
    pyramid: list[Literal["raw", "fact", "topic", "worldview"]] = \
        Field(default_factory=lambda: ["raw", "fact", "topic", "worldview"])
    buffer_no_sync: bool = True                          # 工作记忆不跨脑同步
    archive_retrievability_threshold: float = 0.15       # R 低于此值归档（退出常规召回）


class ThalamusConfig(BaseSettings):
    """丘脑门控：准入、去重、显著性、限流。"""
    model_config = SettingsConfigDict(env_prefix="THAL_")

    min_content_chars: int = 4                           # 过短内容直接拒收
    dedupe_hamming_threshold: int = 3                     # SimHash 汉明距离 ≤3 视为重复
    dedupe_cosine_threshold: float = 0.95                 # 向量近邻去重阈值
    significance_threshold: float = 0.30                  # 低于此值只进缓冲区，不产生跨设备事件
    rate_limit_per_minute: int | None = None              # None → 用 gateway.default_rate_limit
    significance_length_weight: float = 0.3               # 显著性：信息量占比
    significance_pad_weight: float = 0.5                  # 显著性：情绪显著性占比
    significance_confidence_weight: float = 0.2           # 显著性：置信度占比
    significance_length_ref: int = 200                    # 信息量饱和长度（字符）
    dedupe_window: int = 200                              # 近邻去重窗口（最近 N 条）


class AmygdalaConfig(BaseSettings):
    """杏仁核：PAD 精算、好感度低通积分、情绪边界。"""
    model_config = SettingsConfigDict(env_prefix="AMYG_")

    affinity_alpha: float = 0.15                          # 好感度低通系数（越小越慢变）
    pad_smoothing: float = 0.5                            # PAD 与历史均值的平滑比例
    negative_arousal_threshold: float = 0.6               # 高负唤醒触发冷静期
    negative_valence_threshold: float = -0.3              # P 低于此值算负面
    calm_period_ms: int = 6 * 60 * 60 * 1000              # 冷静期时长（6h）
    calm_recall_penalty: float = 0.3                      # 冷静期内该 Agent 记忆的召回折扣


class SyncConfig(BaseSettings):
    """同步平面：GitHub 私有库事件总线。"""
    model_config = SettingsConfigDict(env_prefix="SYNC_")

    repo_url: str = ""                                   # 私库地址（Git PAT 单独存）
    local_repo_dir: Path = DATA_DIR / "brain-sync"
    device_id: str = Field(default_factory=_default_device_id)  # 设备标识（默认取主机名）
    sync_interval_sec: int = 300                         # 定时拉推间隔
    write_debounce_sec: float = 2.0                      # 写入触发防抖
    segment_max_bytes: int = 50 * 1024 * 1024            # 段滚动阈值 ~50MB
    snapshot_monthly: bool = True
    schema_version: int = 1
    use_age_encrypt: bool = False                        # 敏感内容 age 对称加密
    remote_name: str = "origin"


class GatewayConfig(BaseSettings):
    """接入平面：MCP + HTTP 适配器 + 注册中心。"""
    model_config = SettingsConfigDict(env_prefix="GW_")

    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8765
    mcp_token_secret: str = Field(default="", repr=False)  # 令牌签发密钥
    http_host: str = "127.0.0.1"                         # 默认只听本机：MCP 桥接只在本机转发
    http_port: int = 8766
    autostart_http: bool = True                          # 启动即开 HTTP，供多 Agent 的 MCP 转接
    default_rate_limit: int = 60                         # 丘脑按 agent 限速(/min)
    registry_enabled: bool = True


class MergeConfig(BaseSettings):
    """语义合并与聚类摘要（写入裁决 + 夜间升维）。"""
    model_config = SettingsConfigDict(env_prefix="MERGE_")

    merge_enabled: bool = True                    # 写入时由 LLM 裁决 新增/更新/合并/跳过
    merge_cognition_enabled: bool = True          # 带 cognition（认知契约）的写入也参与合并裁决——同事项演进收敛为一条，不再无限追加版本
    merge_candidate_limit: int = 3                # 参与裁决的最相似旧记忆条数
    cluster_summary_enabled: bool = True          # 夜间巩固时为语义聚簇生成主题摘要
    cluster_min_members: int = 3                  # 聚簇至少多少条成员才值得生成摘要


class ActivationConfig(BaseSettings):
    """突触激活：相似度阈值与脉冲映射（前端可视化与后端共用一套参数）。"""
    model_config = SettingsConfigDict(env_prefix="ACT_")

    cache_enabled: bool = True                   # 构图/向量缓存落盘（派生、可随时删掉重建）
    similarity_threshold: float = 0.32          # 余弦≥阈值才画"突触线"
    max_nodes: int = 80                          # 星云视图最多节点
    max_edges_per_node: int = 4                  # 每个节点保留的最强连接数
    pulse_speed_base: float = 0.028             # 基础光点速度（progress/帧）
    pulse_speed_similarity_gain: float = 0.035  # 相似度越高光点越快
    max_pulses: int = 40                        # 同屏光点上限，防止过载


class UiConfig(BaseSettings):
    """前端动画/配色参数（供前端样式参考）。"""
    model_config = SettingsConfigDict(env_prefix="UI_")

    timer_interval_ms: int = 16                 # ~60fps
    background_color: str = "#05080E"
    node_color: str = "#00FFFF"
    emotion_node_color: str = "#FF3264"
    line_color: str = "#64C8FF"
    pulse_color: str = "#FFFFFF"
    normal_node_radius: float = 8.0
    emotion_node_radius: float = 13.0
    center_x: float = 500.0
    center_y: float = 400.0
    center_pull: float = 0.0005                 # 向心力
    repulsion_distance: float = 100.0           # 斥力作用半径
    repulsion_force: float = 0.05
    friction: float = 0.92
    link_distance: float = 180.0                # 视觉近邻连线半径
    proximity_line_alpha: int = 165             # 近邻线基础透明度（语义边更亮）
    pulse_radius: float = 6.0                   # 光点半径
    flash_decay: float = 0.05
    max_labels: int = 6                        # 同屏最多记忆标签（避免重叠）
    drift_jitter: float = 0.35                 # 每帧随机微扰力：节点永不完全静止
    spontaneous_pulse_interval_ms: int = 1200  # 突触自发脉冲间隔（0=关闭）
    neuron_fire_probability: float = 0.0008    # 每帧每节点自发放电概率（微闪烁）
    stardust_count: int = 260                   # 背景星尘点数
    stardust_color: str = "#33496B"
    label_color: str = "#8FA6C4"
    label_font_size: int = 10
    dim_opacity: float = 0.28                   # 高亮时其余节点/连线透明度                   # 终点闪烁衰减
    glow_multiplier: float = 2.5
    window_width: int = 1400
    window_height: int = 900


class MemoryTypeRoutingConfig(BaseSettings):
    """W2 / P1：事务性内容覆盖式写入 vs 认知性内容可追溯写入。"""
    model_config = SettingsConfigDict(env_prefix="ROUTING_")

    enabled: bool = True                         # 总开关，false 时全部走现有 ingest+supersede
    confidence_threshold: float = 0.8           # 低于阈值回退 fact（保留事件溯源）
    transactional_types: list[str] = Field(
        default_factory=lambda: ["code", "draft", "config", "artifact"]
    )
    logical_categories: list[str] = Field(
        default_factory=lambda: ["draft", "config", "artifact"]
    )                                            # 只写 payload.category，不改 MemoryType 枚举
    confirm_user_chat_code_hours: int = 24      # R13：source=user_chat 且 24h 内覆盖需确认
    working_context_max_bytes: int = 8000       # P1.4：代码原文不进长期记忆


class ModelRouterConfig(BaseSettings):
    """W3 / P3：模型路由层。`enabled=false` 时路由层仍可用，只是不把云供应商放进候选池。"""
    model_config = SettingsConfigDict(env_prefix="MODEL_ROUTER_")

    enabled: bool = False                        # B1/Q15：默认关闭，数据不出本机
    sticky_ttl_sec: int = 1800                  # 同 intent 内会话粘性 30min
    request_timeout_sec: int = 20
    privacy_policy: str = "local_only"          # sensitive=true 的记忆只允许 local / paid
    local_endpoint: str = "http://127.0.0.1:11434"
    local_model: str = "qwen2.5:0.5b"
    providers: list[dict[str, Any]] = Field(default_factory=lambda: [])
    intent_map: dict[str, str] = Field(default_factory=lambda: {
        "chat": "fast", "deep_recall": "strong", "emotion": "persona",
        "tool_call": "tools", "consolidation": "strong", "code": "fast",
    })


class SpreadingActivationConfig(BaseSettings):
    """W5 / P4.1：激活扩散。稀疏图自动降 1 跳并告警。"""
    model_config = SettingsConfigDict(env_prefix="SPREAD_")

    enabled: bool = True
    seed_top_k: int = 5
    max_hops: int = 2
    decay: dict[str, float] = Field(default_factory=lambda: {
        "fact": 0.6, "scenario": 0.8, "code": 0.5, "emotion": 0.7, "profile": 0.6,
    })
    threshold: float = 0.15
    max_candidates: int = 40
    timeout_ms: int = 2000                    # 扩散阶段预算；超时回退无扩散 RRF


class PadGatingConfig(BaseSettings):
    """W5 / P4.3 + R4：当前情绪参与召回（分段，不是双权重相加）。"""
    model_config = SettingsConfigDict(env_prefix="PADGATE_")

    enabled: bool = True
    w6: float = 0.4                          # PAD 相似度权重
    arousal_threshold: float = 0.6           # A ≥ 此值且 P 负 → 压制负面记忆
    negative_penalty: float = 0.5
    warmup_turns: int = 5                    # 冷启动前 w6=0


class MediaServiceConfig(BaseSettings):
    """W8 / P6：媒体生成异步旁路（B3：ComfyUI 未验证，首发交付降级路径 + 桩）。"""
    model_config = SettingsConfigDict(env_prefix="MEDIA_")

    enabled: bool = True
    comfyui_url: str = "http://127.0.0.1:8188"
    request_timeout_sec: int = 3
    session_ttl_sec: int = 86400
    max_media_size_mb: int = 2048
    stub_mode: bool = True                    # 真实生成验收放第二阶段


class NarrativeReconstructConfig(BaseSettings):
    """W6 / P5：第一人称叙事重构（白天增量 + 夜间主窗口）。"""
    model_config = SettingsConfigDict(env_prefix="NARRATIVE_")

    enabled: bool = True
    daytime_interval_hours: int = 4         # 白天低频窗口，只处理新增 hyperedge
    max_hyperedges_per_window: int = 50
    min_members: int = 2
    max_retries: int = 3                     # 3 次后标 narrative_pending
    retry_backoff_sec: float = 2.0           # 指数退避基数
    skeleton_chars: int = 200                # A4：叙事故事骨架前缀长度


class HyperedgeBackfillConfig(BaseSettings):
    """W4 / P4.0：从既有记忆补 hyperedge + MEMBER 边，给 W5 的激活扩散铺图。"""
    model_config = SettingsConfigDict(env_prefix="BACKFILL_")

    enabled: bool = True
    similarity_threshold: float = 0.32       # 2-gram 余弦 ≥ 阈值才同簇
    window_hours: int = 72                    # 同 topic 内的时间窗
    min_members: int = 2                      # 成簇最小成员数
    max_clusters: int = 200                   # 单次补边上限，防夜间窗口跑飞
    skeleton_chars: int = 60                  # 成员摘要长度
    sparse_degree_threshold: float = 2.0      # 平均度数低于此值 → 扩散降 1 跳 + 告警


class ChatConfig(BaseSettings):
    """W2 / A2 + W3 / P2：8769 纯消息流。"""
    model_config = SettingsConfigDict(env_prefix="CHAT_")

    enabled: bool = True                         # 本机默认起服务；对外绑定仍需显式 --chat-host 0.0.0.0
    host: str = "127.0.0.1"
    port: int = 8769
    token_file: str = ""                        # chat 专用令牌，空则落 %APPDATA%/super-brain/chat_token.txt
    session_ttl_sec: int = 86400
    max_history_turns: int = 20                 # 会话缓冲只留最近 N 轮原文
    deep_async: bool = True                     # deep 结果从 8769 异步推，不阻塞回复
    persona_channel: str = "bot"                # A5/Q19：聊天必须注入 L0 人格层


class BrainConfig(BaseSettings):
    """全局组合根配置。"""
    # extra=ignore：.env 里的 MODEL_/SYNC_/GW_ 键由子模型消费，不应被顶层判为多余字段
    model_config = SettingsConfigDict(env_prefix="BRAIN_", env_file=".env", extra="ignore")

    app_name: str = "super-brain"
    log_level: str = "INFO"
    cold_start_timeout_sec: int = 30                     # 冷启动<30s
    namespace_policy: Literal["private", "group", "global"] = "private"

    storage: StorageConfig = Field(default_factory=StorageConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    recall: RecallConfig = Field(default_factory=RecallConfig)
    plasticity: PlasticityConfig = Field(default_factory=PlasticityConfig)
    tier: TierConfig = Field(default_factory=TierConfig)
    thalamus: ThalamusConfig = Field(default_factory=ThalamusConfig)
    amygdala: AmygdalaConfig = Field(default_factory=AmygdalaConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    merge: MergeConfig = Field(default_factory=MergeConfig)
    activation: ActivationConfig = Field(default_factory=ActivationConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    memory_type_routing: MemoryTypeRoutingConfig = Field(default_factory=MemoryTypeRoutingConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)
    model_router: ModelRouterConfig = Field(default_factory=ModelRouterConfig)
    hyperedge_backfill: HyperedgeBackfillConfig = Field(default_factory=HyperedgeBackfillConfig)
    spreading_activation: SpreadingActivationConfig = Field(default_factory=SpreadingActivationConfig)
    pad_gating: PadGatingConfig = Field(default_factory=PadGatingConfig)
    narrative_reconstruct: NarrativeReconstructConfig = Field(default_factory=NarrativeReconstructConfig)
    media_service: MediaServiceConfig = Field(default_factory=MediaServiceConfig)


# 单例配置（懒加载于 main.py）
_config: BrainConfig | None = None


def get_config() -> BrainConfig:
    global _config
    if _config is None:
        _config = BrainConfig()
    return _config


def reset_config() -> None:
    """测试 / 重配置时重置单例。"""
    global _config
    _config = None
