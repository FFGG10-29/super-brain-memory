"""gateway.account_pool.catalog —— 号池模型目录（WorkBuddy + 免密 lane）。

移植自 dsh-xdpool / dsh-connect-workbuddy（MIT）：

- WorkBuddy 目录：CN ``GET {chatBase}/v2/enterprises/personal/models``（Bearer）；
  global ``GET {base}/v3/config``（UA=WorkBuddy/5.8.3 WorkBuddy/5.8.3 CLI/2.161.0，
  该 UA 才拿得到完整 62 模型 + 促销徽章）。倍率是"现在的事实"，绝不硬编码——
  静态 fallback 只保留 roster（id/名称/窗口），价格一律以上游实时为准。
- 免密 lane：``GET https://opencode.ai/inference/v1/models``，
  ``Authorization: Bearer public`` + ``opencode/latest/2.0.26/cli`` 指纹 +
  会话头四连同值；listing 混入付费 id，只保留免密 id（``*-free`` / union-alpha /
  space-bunny-free）。能力表（vision/reasoning/窗口）是本地实测表，不是模型卡。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from core.runtime_paths import user_dir

__all__ = [
    "fetch_workbuddy_models",
    "fetch_free_models",
    "load_cached_catalog",
    "save_cached_catalog",
    "is_free_lane",
    "catalog_path",
]

CN_CHAT_BASE = "https://copilot.tencent.com"
CN_MODELS_PATH = "/v2/enterprises/personal/models"
CN_BILLING_BASE = "https://www.codebuddy.cn"
GLOBAL_BASE = "https://www.workbuddy.ai"
GLOBAL_CONFIG_PATH = "/v3/config"
# 该 UA 才拿得到完整产品配置（CLI 频道只有 35 模型、无促销徽章）
PRODUCT_UA = "WorkBuddy/5.8.3 WorkBuddy/5.8.3 CLI/2.161.0"
CLIENT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) WorkBuddyDesktop/5.8.3")

FREE_UPSTREAM_BASE = "https://opencode.ai"
FREE_LISTING_PATH = "/inference/v1/models"
FREE_CLIENT_UA = "opencode/latest/2.0.26/cli"
FREE_LISTING_TIMEOUT_SEC = 15

# 免密 id 规则（照抄 free-catalog.ts）：无 -free 后缀的免密 id
_ALWAYS_FREE = {"union-alpha", "space-bunny-free"}

# 客户端的档位选择器（快速/均衡/极致）。这三个 id 不在 cli 清单里，
# 但客户端下拉把它们单独渲染成一档，必须单独取。
TIER_MODEL_IDS: tuple[str, ...] = ("fast-model", "balanced-model", "deep-model")


def client_roster(payload: dict[str, Any]) -> list[str]:
    """客户端**实际展示**的具名模型清单。

    ★ 数量口径铁律（2026-11-12 查证）：`/v3/config` 与
    `/v2/enterprises/personal/models` 的 `data.models` 都是**全量配置**——
    含 IDE 补全（codewise-*）、视频/图像（kling-video、hunyuan-image）、
    内部路由变体（hy3-b/c、hy4-preview-ctrl/exp1/exp2、*-lkeap、*-taiji、
    *-taco-completion）、旧版别名（default-1.1/1.2），CN 68 条、global 37 条。
    客户端模型选择器只渲染 `data.agents[name=cli].models`（实测 CN 16 个）。
    照全量清单铺出来就是"64 个模型"的假象，与用户所见客户端完全不符。

    所以：**有 roster 就按 roster 的顺序和成员输出**，多余的 id 一律不展示。
    取不到 roster（老接口/异常 payload）时才退回全量——宁可多不可错删。
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    agents = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(agents, list):
        return []
    preferred: list[str] = []
    fallback: list[str] = []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        ids = agent.get("models")
        if not isinstance(ids, list) or not ids:
            continue
        clean = [str(i) for i in ids if str(i).strip()]
        tags = agent.get("tags") if isinstance(agent.get("tags"), list) else []
        if agent.get("name") == "cli" or "default" in tags:
            preferred = clean
            break
        if not fallback:
            fallback = clean
    return preferred or fallback


def _multiplier_overlay(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """/v3/config → {id: {multiplier, promotion, efforts, default_effort}}。

    /v2 的模型行**没有 credits 字段**，倍率只能从 /v3 取；取不到就保持 None
    并显示"倍率未取到"，绝不填 0（0 等于宣称免费，是最贵的一种谎）。
    """
    try:
        rows = _parse_wb_models(payload, use_roster=False)
    except Exception:  # noqa: BLE001
        return {}
    return {str(r["id"]): r for r in rows if r.get("id")}



def is_free_lane(model_id: str) -> bool:
    base = _base_model_id(model_id)
    if base in _ALWAYS_FREE:
        return True
    return re.search(r"(?:^|[-_])free(?:$|[-_.])", base) is not None


def _base_model_id(model: str) -> str:
    return re.sub(r"\([^()]+\)\s*$", "", model).strip()


# 免密能力基线（本地实测表，逐条移植 CAPABILITIES）
_FREE_CAPABILITIES: tuple[tuple[str, dict[str, Any]], ...] = (
    (r"^mimo.*v2\.6", dict(vision=True, reasoning=True, context=1_048_576, max_output=131_072)),
    (r"^mimo.*v2\.5", dict(vision=True, reasoning=True, context=1_048_576, max_output=131_072)),
    (r"^mimo", dict(vision=True, reasoning=True, context=262_144, max_output=131_072)),
    (r"^muse.?spark", dict(vision=True, reasoning=True, context=1_048_576, max_output=131_072)),
    (r"^nemotron", dict(vision=False, reasoning=True, context=128_000, max_output=32_768)),
    (r"^ling", dict(vision=False, reasoning=True, context=128_000, max_output=32_768)),
    (r"^space.?bunny", dict(vision=True, reasoning=True, context=262_144, max_output=65_536)),
    (r"^union", dict(vision=True, reasoning=False, context=262_144, max_output=131_072)),
    (r"^deepseek", dict(vision=False, reasoning=True, context=128_000, max_output=64_000)),
    (r"^jev", dict(vision=False, reasoning=False, context=32_768, max_output=4_096)),
)
_FREE_FALLBACK = dict(vision=False, reasoning=True, context=131_072, max_output=32_768)

# 上游 id → 展示名（照抄 DISPLAY_NAMES）
_FREE_DISPLAY_NAMES = {
    "mimo-v2.6-flash-free": "MiMo V2.6 Flash",
    "mimo-v2.5-free": "MiMo V2.5",
    "muse-spark-1.3-contributor-free": "Muse Spark 1.3",
    "muse-spark-1.2-contributor-free": "Muse Spark 1.2",
    "nemotron-3-ultra-free": "Nemotron 3 Ultra",
    "nemotron-3.5-lightning-free": "Nemotron 3.5 Lightning",
    "ling-3.0-flash-fin-free": "Ling 3.0 Flash Fin",
    "space-bunny-free": "Space Bunny",
    "union-alpha": "Union Alpha",
    "deepseek-v4-flash-free": "DeepSeek V4 Flash",
    "jev-1.13-free": "Jev 1.13",
}

# 免密 lane 已按用户要求彻底删除（2026-11-12）：静态兜底清单清空。
# 原 11 个模型不再出现在任何目录/下拉/缓存；保留空元组仅为兼容外部 import。
FALLBACK_FREE_IDS: tuple[str, ...] = ()

# WorkBuddy 静态兜底 roster（只含 id/名称/窗口，绝不带价格）
FALLBACK_WB_MODELS_CN = (
    ("hy3", "Hy3", 192_000), ("glm-5v-turbo", "GLM-5v-Turbo", 200_000),
    ("glm-5.3", "GLM-5.3", 1_000_000), ("glm-5.2", "GLM-5.2", 1_000_000),
    ("glm-5.1", "GLM-5.1", 200_000), ("minimax-m3", "MiniMax-M3", 512_000),
    ("kimi-k3-1", "Kimi-K3", 1_000_000), ("kimi-k2.7", "Kimi-K2.7-Code", 256_000),
    ("kimi-k2.6", "Kimi-K2.6", 256_000),
    ("deepseek-v4-flash", "Deepseek-V4-Flash", 1_000_000),
    ("deepseek-v4-pro", "Deepseek-V4-Pro", 1_000_000),
)
FALLBACK_WB_MODELS_GLOBAL = (
    ("fast-model", "Fast", 200_000), ("balanced-model", "Balanced", 256_000),
    ("primary-model", "Primary", 272_000), ("deep-model", "Deep", 176_000),
    ("deepseek-v4.1-flash", "Deepseek-V4.1-Flash", 1_000_000),
    ("gpt-6-astra", "GPT-6-Astra", 1_000_000), ("hy4-preview", "Hy4 preview", 1_000_000),
    ("hy3", "Hy3", 192_000), ("gpt-5.6-sol", "GPT-5.6-Sol", 1_000_000),
    ("gpt-5.6-terra", "GPT-5.6-Terra", 1_000_000), ("gpt-5.6-luna", "GPT-5.6-Luna", 1_000_000),
    ("gpt-5.5", "GPT-5.5", 1_000_000), ("gpt-5.4", "GPT-5.4", 272_000),
    ("gpt-5.3-codex", "GPT-5.3-Codex", 272_000),
    ("gemini-3.5-flash", "Gemini-3.5-Flash", 1_000_000),
    ("glm-5.3", "GLM-5.3", 1_000_000), ("glm-5.2", "GLM-5.2", 1_000_000),
    ("kimi-k3", "Kimi-K3", 1_000_000), ("kimi-k2.6", "Kimi-K2.6", 256_000),
)


def _http_json(url: str, headers: dict[str, str], body: bytes = b"",
               timeout: int = 20) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, data=body if body else None, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8") or "{}")
        return response.status, (payload if isinstance(payload, dict) else {})


def _display_free_name(model_id: str) -> str:
    known = _FREE_DISPLAY_NAMES.get(model_id)
    if known:
        return known
    return re.sub(r"[-_.]+", " ", model_id).strip().title()


def _free_capability(model_id: str) -> dict[str, Any]:
    for pattern, caps in _FREE_CAPABILITIES:
        if re.search(pattern, model_id):
            return dict(caps)
    return dict(_FREE_FALLBACK)


# ── 免密 lane listing ─────────────────────────────────────────────────────
def _free_session() -> str:
    from gateway.account_pool.pool import mint_session_id

    return mint_session_id()


def fetch_free_models() -> dict[str, Any]:
    """免密 lane 已按用户要求彻底移除（2026-11-12）。

    不再请求 opencode.ai、不再产出任何免密免费模型。返回空列表让所有下游
    （catalog 缓存 / 号池页免密区 / 模型配置下拉 / 聊天页下拉）自动消失。
    硅基流动 15 个免费模型走 providers_registry.json 的 SiliconFlow 条目，
    与这条已删除的免密 lane 无关。
    """
    return {"ok": True, "models": [], "live": False, "disabled": True}


def _free_row(base_id: str) -> dict[str, Any]:
    caps = _free_capability(base_id)
    return {
        "id": base_id,
        "name": _display_free_name(base_id),
        "wire": _free_wire(base_id),
        "vision": caps["vision"], "reasoning": caps["reasoning"],
        "context_window": caps["context"], "max_output": caps["max_output"],
        "region_sensitive": bool(re.search(r"^muse.?spark", base_id)),
        "multiplier": 0.0, "image": caps["vision"],
        "efforts": [], "default_effort": "", "can_disable_thinking": False,
        "is_default": False, "vendor": "opencode-free", "promo_free": True,
    }


def _free_wire(model_id: str) -> str:
    base = model_id.split("/")[-1]
    if re.search(r"^muse[-_]?spark(?:$|[-_:.\s])", base, re.IGNORECASE) \
            or base.startswith(("o3", "gpt-5-codex")):
        return "responses"
    if re.search(r"^union", base, re.IGNORECASE):
        return "messages"
    return "chat"


# ── WorkBuddy 目录 ────────────────────────────────────────────────────────
def _parse_multiplier(credits: Any) -> float | None:
    """credits 字段：实测是 "x0.69" / "x0.79 credits" 字符串（也可能是数值）。

    /v2 的模型行里带单位后缀（"x0.79 credits"），直接 float() 会失败并回落成
    None——那会把真实倍率全变成"未取到"。用正则取第一个数字。
    """
    if credits is None:
        return None
    if isinstance(credits, (int, float)):
        return float(credits)
    match = re.search(r"-?\d+(?:\.\d+)?", str(credits))
    return float(match.group()) if match else None


def _parse_wb_models(payload: dict[str, Any], use_roster: bool = True) -> list[dict[str, Any]]:
    """解析 /v3/config 产品配置（实测 2026-11：CN 68 模型 + modelTiers + 促销）。

    上游包 envelope {code, msg, requestId, data}；模型在 data.models。
    每模型保留：id/name/倍率(credits)/上下文/多模态(supportsImages 且未被
    disabledMultimodal 否决)/思考档位(reasoning.supportedEfforts)。

    ★ 免费判定铁律（2026-11-12 修，照抄 dsh-connect-workbuddy catalog.ts:88-92）：
      `promo_free` **只认 credits 真实倍率 == 0**，绝不采信上游 modelPromotions 标记
      或"限时免费"广告。实测上游 Channels 把正在扣费的 DeepSeek-V4.1-Flash（x0.11）、
      Deepseek-V4-Pro（x0.51）、kimi-k3-2（x1.62）都塞进促销名单——把收费模型标成免费
      是这份表能犯的最贵的错（wbmod 原话："a captured free is the most expensive
      thing this table could claim"）。促销另存 `promotion` 字段仅供展示，不影响免费判定。
      倍率缺失（None）一律视为**收费**（CN 静态兜底本就没有倍率字段），不当免费。
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    raw_models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(raw_models, list):
        return []
    # badge 标签仅展示，不参与 promo_free 免费判定（见 docstring 铁律）。
    # 真实结构：data.modelPromotions[].badge.label（限时免费/夜间折扣/限时折扣），
    # 以及 data.modelTiers[].badge.label（订阅优先）。必须按 enabled + schedule
    # 判断当前是否生效，否则过期活动（如 10 月的 space-bunny 折扣）会一直挂着。
    badge_label: dict[str, str] = {}
    badge_color: dict[str, str] = {}
    badge_priority: dict[str, int] = {}

    def _promo_active(promo: dict[str, Any]) -> bool:
        if promo.get("enabled") is False:
            return False
        sched = promo.get("schedule")
        if not isinstance(sched, dict):
            return True
        now = time.time()
        # 绝对有效期（带时区，fromisoformat 能解析 +08:00）
        for k in ("validFrom", "validUntil"):
            raw = sched.get(k)
            if raw:
                try:
                    ts = datetime.fromisoformat(str(raw)).timestamp()
                except (ValueError, TypeError):
                    continue
                if k == "validFrom" and now < ts:
                    return False
                if k == "validUntil" and now > ts:
                    return False
        # 每日时段（活动只在特定时段亮 badge；非时段可能退化为另一 badge，取优先级高的）
        if sched.get("timezone"):
            return True  # 有 tz 但只有 daily 时段的，交给调用方按优先级取舍
        return True

    def _put_badge(mid: str, label: str, color: str, priority: int) -> None:
        if not mid or not label:
            return
        # 同模型多 badge：priority 高者胜（限时免费 200 > 夜间折扣 100 > 订阅优先 20）
        if mid not in badge_priority or priority > badge_priority[mid]:
            badge_priority[mid] = priority
            badge_label[mid] = label
            badge_color[mid] = color

    promotions = data.get("modelPromotions") if isinstance(data, dict) else None
    if isinstance(promotions, list):
        for promo in promotions:
            if not isinstance(promo, dict) or not isinstance(promo.get("modelIds"), list):
                continue
            if not _promo_active(promo):
                continue
            badge = promo.get("badge") if isinstance(promo.get("badge"), dict) else {}
            label = str(badge.get("label") or badge.get("shortLabel") or "")
            color = str(badge.get("color") or "")
            priority = int(promo.get("priority") or 0)
            for mid in promo["modelIds"]:
                _put_badge(str(mid), label, color, priority)

    tiers = data.get("modelTiers") if isinstance(data, dict) else None
    if isinstance(tiers, list):
        for tier in tiers:
            if not isinstance(tier, dict) or tier.get("enabled") is False:
                continue
            if not isinstance(tier.get("modelIds"), list):
                continue
            badge = tier.get("badge") if isinstance(tier.get("badge"), dict) else {}
            label = str(badge.get("label") or "")
            color = str(badge.get("color") or "")
            priority = int(tier.get("priority") or 20)
            for mid in tier["modelIds"]:
                _put_badge(str(mid), label, color, priority)

    # ★ 客户端口径（2026-11-12）：只输出 roster 里的模型。
    #   /v3/config 的 data.models 是全量配置——CN 68 条，混着 IDE 补全
    #   （codewise-*）、视频/图像（kling-video、hunyuan-image）、内部路由变体
    #   （hy3-b/c、hy4-preview-ctrl/exp1/exp2、*-lkeap、*-taiji、*taco-completion）、
    #   旧别名（default-1.1/1.2）。这些**不是用户能选的模型**，全量铺出去就是
    #   "64 个模型"的假象。客户端选择器只渲染 data.agents[cli].models（16 个），
    #   本函数只认这个清单 + 三个档位（快速/均衡/极致）。
    #   roster 取不到时才退回全量（宁可多不可错删）。
    roster = client_roster(payload) if use_roster else []
    by_id: dict[str, dict[str, Any]] = {}
    raw_list: list[dict[str, Any]] = []
    for row in raw_models:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or "").strip()
        if not mid:
            continue
        raw_list.append(row)
        by_id.setdefault(mid, row)
    if roster:
        ordered = [m for m in TIER_MODEL_IDS if m in by_id]
        ordered += [m for m in roster if m not in TIER_MODEL_IDS]
        # roster 里可能有本响应没给的 id（/v3 的 roster 含 kimi-k3-2、hy3-x 等），
        # 缺模型的直接跳过——没有能力数据不硬编一行空壳。
        rows = [by_id[m] for m in ordered if m in by_id]
    else:
        rows = raw_list

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        model_id = str(row.get("id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        multiplier = _parse_multiplier(row.get("credits"))
        reasoning = row.get("reasoning") if isinstance(row.get("reasoning"), dict) else {}
        efforts = reasoning.get("supportedEfforts")
        out.append({
            "id": model_id,
            "name": str(row.get("name") or model_id),
            "multiplier": multiplier,
            "context_window": int(row.get("maxInputTokens") or 0) or None,
            "max_output": int(row.get("maxOutputTokens") or 0) or None,
            "image": bool(row.get("supportsImages"))
                     and not bool(row.get("disabledMultimodal")),
            "reasoning": bool(row.get("supportsReasoning") or row.get("onlyReasoning")),
            "default_effort": str(reasoning.get("defaultEffort") or ""),
            "efforts": [str(e) for e in efforts] if isinstance(efforts, list) else [],
            "can_disable_thinking": reasoning.get("canDisableThinking") is True,
            "is_default": bool(row.get("isDefault")),
            "vendor": str(row.get("vendor") or ""),
            # 免费只认真实倍率 0；倍率缺失(None)按收费处理，绝不因促销名单误标免费
            "promo_free": multiplier == 0.0,
            # 促销徽章（badge.label 实测才是真标签；旧代码取 promo.get("name")
            # 永远落空，且引用了未定义的 promo_label/promo_ids，一跑就 NameError）
            "promotion": badge_label.get(model_id, ""),
            "promotion_color": badge_color.get(model_id, ""),
            # 档位选择器（快速/均衡/极致）单独标记，前端置顶渲染
            "tier": model_id in TIER_MODEL_IDS,
        })
    return out


def fetch_workbuddy_models(region: str, access_token: str = "",
                           domain: str = "") -> dict[str, Any]:
    """WorkBuddy 实时目录，**按客户端可见口径**输出。

    CN 走客户端自己的 `/v2/enterprises/personal/models`：`data.models` 是套餐
    全部条目，`data.agents[cli].models` 才是选择器真正渲染的清单（16 个）；
    `_parse_wb_models` 按后者过滤，再加三个档位（快速/均衡/极致）。倍率就在
    这个响应的 `credits` 字段里（形如 "x0.79 credits"），不需要另找。
    `/v3/config` 只补促销徽章（限时免费/夜间折扣/订阅优先）与思考档。

    global 仍走 `/v3/config`（同结构，payload 里有 agents 就自动按 roster 过滤）。
    失败回落静态 roster（只 roster，无价格——价格是"现在的事实"）。
    """
    is_global = region == "global" or domain.lower().endswith(
        ("workbuddy.ai", "codebuddy.ai"))
    try:
        if is_global:
            base = "https://www.codebuddy.ai" if domain.lower().endswith("codebuddy.ai") \
                else GLOBAL_BASE
            headers = {"user-agent": PRODUCT_UA, "accept": "application/json"}
            _status, payload = _http_json(base + GLOBAL_CONFIG_PATH, headers)
        else:
            headers = {
                "accept": "application/json", "user-agent": PRODUCT_UA,
                "origin": CN_CHAT_BASE, "referer": CN_CHAT_BASE + "/",
            }
            if access_token:
                headers["authorization"] = f"Bearer {access_token}"
            # ★ CN 主清单走客户端自己的接口：/v2/enterprises/personal/models。
            #   它的 data.models 是套餐内全部条目（37），而 data.agents[cli].models
            #   才是客户端选择器真正渲染的清单（16）——_parse_wb_models 会按它过滤。
            #   /v3/config 只用来补倍率与促销徽章（/v2 的模型行没有 credits 字段），
            #   拿不到就保持"倍率未取到"，绝不填 0 冒充免费。
            _status, payload = _http_json(CN_CHAT_BASE + CN_MODELS_PATH, headers)
            models = _parse_wb_models(payload)
            try:
                _s2, cfg = _http_json(CN_CHAT_BASE + GLOBAL_CONFIG_PATH, headers)
                overlay = _multiplier_overlay(cfg)
            except Exception:  # noqa: BLE001  促销/倍率是增强项，失败不阻断目录
                overlay = {}
            for model in models:
                extra = overlay.get(model["id"]) or {}
                if not model["promotion"]:
                    model["promotion"] = extra.get("promotion") or ""
                    model["promotion_color"] = extra.get("promotion_color") or ""
                # 上游会把正在扣费的模型挂"限时免费"徽章：倍率不为 0 时降级成
                # "促销中"，绝不让界面出现"免费"字样而实际扣费。
                if "免费" in model["promotion"] and model["promo_free"] is not True:
                    model["promotion"] = "促销中"
                if not model["efforts"]:
                    model["efforts"] = extra.get("efforts") or []
                if not model["default_effort"]:
                    model["default_effort"] = extra.get("default_effort") or ""
                if not model["can_disable_thinking"]:
                    model["can_disable_thinking"] = bool(extra.get("can_disable_thinking"))
                model["promo_free"] = model["multiplier"] == 0.0
            if models:
                return {"ok": True, "models": models, "live": True}
        models = _parse_wb_models(payload)
        if models:
            return {"ok": True, "models": models, "live": True}
        raise RuntimeError("目录为空")
    except Exception as exc:  # noqa: BLE001  回落静态 roster
        fallback = FALLBACK_WB_MODELS_GLOBAL if is_global else FALLBACK_WB_MODELS_CN
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "models": [{"id": mid, "name": name, "multiplier": None,
                            "context_window": ctx, "max_output": None,
                            "image": False, "reasoning": False,
                            "efforts": [], "default_effort": "",
                            "can_disable_thinking": False, "is_default": False,
                            "vendor": "", "promo_free": False}
                           for mid, name, ctx in fallback],
                "live": False}


# ── 目录持久化 ────────────────────────────────────────────────────────────
# ── 云供应商注册表（xdpool providers-registry.json 原样移植，77 家）────────
def provider_registry() -> list[dict[str, Any]]:
    """77 家 API 供应商：{id,name,url,baseUrl,model_count,first_model,models}。"""
    path = Path(__file__).with_name("providers_registry.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data if isinstance(data, list) else data.get("providers") or []
    out: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        models = row.get("models") or []
        out.append({
            "id": str(row["id"]),
            "name": str(row.get("name") or row["id"]),
            "url": str((row.get("auth") or {}).get("url") or ""),
            "base_url": str(row.get("baseUrl") or ""),
            "format": str(row.get("format") or "openai-chat"),
            "model_count": len(models),
            # 带 category/contextWindow，供前端按用途过滤（精排处只列 rerank 模型）。
            "models": [{
                "id": str(m.get("id") or ""),
                "name": str(m.get("name") or m.get("id") or ""),
                "category": str(m.get("category") or ""),
                "contextWindow": m.get("contextWindow"),
                "vision": m.get("vision"),
                "reasoning": m.get("reasoning"),
            } for m in models if isinstance(m, dict) and m.get("id")],
        })
    return out


def catalog_path() -> Path:
    return user_dir() / "pool" / "catalog.json"


def load_cached_catalog() -> dict[str, Any] | None:
    path = catalog_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        # 免密 lane 已删除：旧缓存里的 11 个 free 模型一律屏蔽，避免历史缓存复活。
        free = data.get("free")
        if isinstance(free, dict) and free.get("models"):
            data["free"] = {**free, "models": [], "disabled": True}
        # ★ 旧口径缓存作废（2026-11-12）：改造前缓存的是 /v3/config 全量清单
        #   （CN 68 条，含 codewise-/video/image/内部变体），与客户端可见的
        #   16 个具名 + 3 档位完全不符。凡是模型行缺少 `tier` 键的，一律视为
        #   旧格式丢弃、触发重拉——否则用户在界面上会一直看到那 64 行。
        wb = data.get("wb")
        if isinstance(wb, dict) and isinstance(wb.get("models"), list) and wb["models"]:
            if not all(isinstance(m, dict) and "tier" in m for m in wb["models"]):
                return None
        return data
    except (ValueError, OSError):
        return None


def save_cached_catalog(data: dict[str, Any]) -> None:
    path = catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {**data, "saved_at_ms": int(time.time() * 1000)}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
