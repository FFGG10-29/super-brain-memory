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

# 免密 lane 已知在服的静态兜底（listing 不可达时保底，绝不空白）
FALLBACK_FREE_IDS = (
    "muse-spark-1.3-contributor-free", "muse-spark-1.2-contributor-free",
    "union-alpha", "space-bunny-free", "nemotron-3-ultra-free",
    "deepseek-v4-flash-free", "mimo-v2.6-flash-free", "mimo-v2.5-free",
    "jev-1.13-free",
)

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
    """拉免密 listing，过滤免密 id，产出 FreeModelRow 列表。"""
    session = _free_session()
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": "Bearer public",
        "user-agent": FREE_CLIENT_UA,
        "x-opencode-client": "cli",
        "x-opencode-project": "global",
    }
    for name in ("x-opencode-session", "x-opencode-session-id",
                 "x-session-affinity", "x-session-id"):
        headers[name] = session
    try:
        _status, payload = _http_json(
            FREE_UPSTREAM_BASE + FREE_LISTING_PATH, headers,
            timeout=FREE_LISTING_TIMEOUT_SEC)
        rows = payload.get("data") or payload.get("models") or []
    except Exception as exc:  # noqa: BLE001  网络失败回落静态兜底
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "models": [ _free_row(mid) for mid in FALLBACK_FREE_IDS ],
                "live": False}
    ids: list[str] = []
    for row in rows:
        value = row if isinstance(row, str) else (row or {}).get("id") if isinstance(row, dict) else None
        if isinstance(value, str) and value:
            ids.append(value)
    seen: set[str] = set()
    models: list[dict[str, Any]] = []
    for raw in ids:
        if not is_free_lane(raw):
            continue
        base = _base_model_id(raw)
        if base in seen:
            continue
        seen.add(base)
        models.append(_free_row(base))
    if not models:
        return {"ok": False, "error": "gateway listing 无免密模型",
                "models": [_free_row(mid) for mid in FALLBACK_FREE_IDS],
                "live": False}
    return {"ok": True, "models": models, "live": True}


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
    """credits 字段：实测是 "x0.69" 字符串（也可能是数值）；缺省 None=未公布。"""
    if credits is None:
        return None
    if isinstance(credits, (int, float)):
        return float(credits)
    text = str(credits).strip().lstrip("xX×")
    try:
        return float(text)
    except ValueError:
        return None


def _parse_wb_models(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 /v3/config 产品配置（实测 2026-11：CN 68 模型 + modelTiers + 促销）。

    上游包 envelope {code, msg, requestId, data}；模型在 data.models。
    每模型保留：id/name/倍率(credits)/上下文/多模态(supportsImages 且未被
    disabledMultimodal 否决)/思考档位(reasoning.supportedEfforts)/
    限时免费(promo 或倍率 0)。字段缺就留空，不虚构。
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    raw_models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(raw_models, list):
        return []
    promo_ids: set[str] = set()
    promotions = data.get("modelPromotions") if isinstance(data, dict) else None
    if isinstance(promotions, list):
        for promo in promotions:
            if isinstance(promo, dict) and isinstance(promo.get("modelIds"), list):
                promo_ids.update(str(mid) for mid in promo["modelIds"] if mid)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in raw_models:
        if not isinstance(row, dict):
            continue
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
            "promo_free": (multiplier == 0.0) or (model_id in promo_ids),
        })
    return out


def fetch_workbuddy_models(region: str, access_token: str = "",
                           domain: str = "") -> dict[str, Any]:
    """WorkBuddy 实时目录：双区都走 /v3/config（实测 CN 68 模型，带倍率/思考档/促销）。

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
            _status, payload = _http_json(CN_CHAT_BASE + GLOBAL_CONFIG_PATH, headers)
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
            "models": [{"id": str(m.get("id") or ""), "name": str(m.get("name") or m.get("id") or "")}
                       for m in models if isinstance(m, dict) and m.get("id")],
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
        return data if isinstance(data, dict) else None
    except (ValueError, OSError):
        return None


def save_cached_catalog(data: dict[str, Any]) -> None:
    path = catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {**data, "saved_at_ms": int(time.time() * 1000)}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
