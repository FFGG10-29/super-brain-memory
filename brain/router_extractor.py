"""brain.router_extractor —— 感知：LLM 严格 JSON 抽取。

输出契约（`_SYSTEM_PROMPT` 与 `Extraction` 一一对应）：
    memory_type / entities / relations / hyperedge / pad / confidence

降级：LLM 不可用或输出非法时走**规则抽取**（`_rule_extract`），
PAD 用情绪词典估算 —— 保证"每条记忆都带 PAD"这一全链路不变量，
但 `source` 字段明确标注是 rule 而非 llm，便于 eval 区分质量。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from config import BrainConfig, get_config
from core.errors import EngineUnavailable
from core.interfaces import LocalLLM, MemoryType
from models.neuron import PAD, Neuron

__all__ = ["Extraction", "ExtractedEntity", "ExtractedRelation", "ExtractionBundle",
           "RouterExtractor"]

# 一进多出：一条对话 → 一组结构化记忆（profile/emotion/fact/scenario + 事件骨架）。
# 与单条 Extraction 正交，不影响既有 extract() 契约。
_MULTI_SYSTEM_PROMPT = """你是记忆结构化抽取器。**拆解粒度由信息密度决定，绝不是所有输入都拆成 5 类**。只输出一个 JSON 对象，不要解释、不要围栏。

## 第 0 步 · 分流（先判属于哪个梯队）
【事务性】含大段代码/文件路径/编译报错/配置键值对，或指令是"改这段代码/写脚本/保存配置" → {"transactional":true,"summary":"一句摘要","complexity":"none","items":[],"event":null}。剥夺参与叙事权。
【日常寒暄/系统指令】"早上好""清空上下文""在吗" 等无信息量的寒暄 → {"ephemeral":true,"complexity":"none","items":[],"event":null}。不进六引擎，走 working-context 丢弃。
【认知性】其余 → 进入下面按信息密度分级拆解。

## 第 1 步 · 认知性分级（终极判据：这段记忆能不能撑起一段"叙事"？）
- light 轻量（撑不起故事/像日记缺博弈）：客观知识问答（"FastAPI 怎么开 CORS"）→ 只 1 条 fact；单向倾诉（"客户又改需求气死我了"）→ scenario+emotion 2 条；客观+偏好（"换 Mac 了屏幕真舒服"）→ fact+profile 2 条。
- heavy 重度（能撑起丰满故事）：满足以下【任意 2 条】才启动 3~5 类全拆：① 有明确交互对象（用户↔大肥鱼/用户↔xx）；② 有情绪起伏（语气词/暧昧/冲突/开心 PAD 波动）；③ 有附图/语音媒体；④ 暴露新的长期偏好、关系变化或重要事实。

## 第 2 步 · 输出（light 只给必要的几条，heavy 才给全套）
{
  "transactional": false, "ephemeral": false,
  "complexity": "light|heavy",
  "media_attachments": ["路径"] 或 [],
  "items": [
    {"memory_type":"fact|scenario|emotion|profile",
     "content":"规范化陈述（第三人称，非原文照抄）",
     "requires_hyperedge": true|false,
     "pad":{"p":-1~1,"a":-1~1,"d":-1~1},
     "confidence":0~1,
     "entities":[{"name":"实体名","type":"person|project|tool|concept"}]}
  ],
  "event": {"event_summary":"事件一句话","members":["实体名",...]} 或 null
}

## 四类边界（按时间跨度+抽象层级）
1. scenario 情景（骨架·动词·"谁在何时做了什么"）：特定时间/地点/参与者的交互过程，几分钟~几小时。是超边主体。仅交互类 scenario 标 requires_hyperedge=true。
2. fact 事实（客观结论·名词/状态·"X 是什么/在哪"）：脱离情境也成立。与 scenario 重叠（如"xx搬家"）→ 拆两条：fact 存"xx在搬家（长期状态）"、scenario 存"我问xx要不要帮忙（具体交互）"。
3. profile 画像（灵魂·形容词/偏好·跨情景稳定、不含时间点）：单次提取 confidence 必须 ≤0.7（多次出现才固化，由后处理升维）。
4. emotion 情绪（颜色·情绪词·必须带时间戳、必须依附某 scenario）：无对应 scenario 的"无根情绪"不要输出；pad 必填三数值。

## 互斥/包含 + 超边规则
- 偏好只写 profile 不写 fact；不为拆而拆；最多 5 条；profile 不进 event.members。
- hyperedge 仅当 items 里存在 requires_hyperedge=true 的交互 scenario 且 ≥2 主体时才给，否则 event=null。fact/emotion 作为属性挂超边，profile 全局不进超边。
- pad：emotion 必填；高唤醒 scenario 必填；其余可给中性非零。

总纲：fact 骨架、scenario 血肉、profile 灵魂、emotion 颜色；超边连骨架血肉、情绪上色、画像夜间固化。"""

_MULTI_FALLBACK_PROMPT = _MULTI_SYSTEM_PROMPT

_SYSTEM_PROMPT = """你是记忆抽取器。只输出一个 JSON 对象，不要解释、不要 Markdown 围栏。
字段：
{
  "memory_type": "fact|code|scenario|emotion|profile",
  "entities": [{"name": "实体名", "type": "person|project|tool|concept"}],
  "relations": [{"subject": "实体名", "predicate": "关系动词", "object": "实体名"}],
  "hyperedge": {"label": "事件标签", "members": ["实体名"]} 或 null,
  "pad": {"p": -1~1 的愉悦度, "a": -1~1 的唤醒度, "d": -1~1 的支配度},
  "confidence": 0~1 的置信度
}
规则：pad 必须给出三个数值；不确定时置信度低于 0.6。"""


@dataclass(frozen=True)
class ExtractedEntity:
    name: str
    type: str = "concept"


@dataclass(frozen=True)
class ExtractedRelation:
    subject: str
    predicate: str
    object: str


@dataclass(frozen=True)
class Extraction:
    memory_type: MemoryType
    pad: PAD
    confidence: float
    source: str = "llm"                                  # llm | rule
    entities: tuple[ExtractedEntity, ...] = field(default_factory=tuple)
    relations: tuple[ExtractedRelation, ...] = field(default_factory=tuple)
    hyperedge_label: str | None = None
    hyperedge_members: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ExtractionItem:
    """一进多出里的单条：content 已规范化，不再是原文照抄。"""
    memory_type: MemoryType
    content: str
    pad: PAD
    confidence: float
    entities: tuple[ExtractedEntity, ...] = field(default_factory=tuple)
    requires_hyperedge: bool = False   # 仅交互类 scenario 为 True（超边组建前提）


@dataclass(frozen=True)
class ExtractionBundle:
    """一条输入拆成的一组记忆 + 一张可选事件骨架；或事务性摘要（走 overwrite 通道）。"""
    items: tuple[ExtractionItem, ...]
    event_label: str | None = None
    event_members: tuple[str, ...] = field(default_factory=tuple)
    source: str = "llm"
    transactional: bool = False            # 第一道边界：事务性→overwrite，不进认知池/超边/叙事
    summary: str = ""                       # 事务性摘要（"重构了用户模块"）
    raw_content: str = ""                   # 原文（事务性 overwrite 用）
    ephemeral: bool = False                 # 第三梯队·寒暄/系统指令→working-context，不进六引擎
    complexity: str = "light"               # none | light | heavy（信息密度分级，决定拆解粒度）
    media_attachments: tuple[str, ...] = field(default_factory=tuple)  # 媒体附件路径（已剥离）
    # ★ 叙事载体（2026-11-12 架构裁决）：flush 提纯出的第一人称叙事是抽取器的
    #   "输入材料"，拆解后作为 scenario 的 narrative 属性随组落库（LMDB 旁路
    #   索引），绝不整段直接当记忆正文——图谱靠碎片节点生长，不靠长文本。
    narrative: str = ""
    # 组内条目的分层语义：聊天情节=episode（默认）；蒸馏知识=fact（蒸馏器覆写）。
    layer: str = "episode"


# 规则词典：只在 LLM 不可用时使用，命中即调 PAD（中文情绪词典简化版）
_NEGATIVE = ("讨厌", "崩溃", "生气", "吵", "冲突", "失败", "报错", "糟糕", "烦", "拒绝", "骂")
_POSITIVE = ("喜欢", "感谢", "顺利", "开心", "搞定", "漂亮", "赞", "成功", "通过", "舒服")
_HIGH_AROUSAL = ("!", "！", "赶紧", "马上", "立刻", "急", "崩溃", "吵", "炸", "救命")
_LOW_AROUSAL = ("可能", "也许", "大概", "顺便", "有空")
_CODE_HINTS = ("def ", "class ", "函数", "报错", "堆栈", "commit", "bug", "编译", "测试")
_PROFILE_HINTS = ("我是", "我叫", "我的习惯", "我偏好", "我不喜欢", "记住我",
                  "喜欢", "讨厌", "爱用", "换", "屏幕", "看着真", "最爱", "惯用")
_SCENARIO_HINTS = ("那天", "当时", "后来", "昨天", "今天", "上次", "会议")


class RouterExtractor:
    def __init__(self, llm: LocalLLM, config: BrainConfig | None = None) -> None:
        self._llm = llm
        self._config = config or get_config()

    def from_agent(self, cognition: dict[str, Any]) -> Extraction:
        """★ 认知契约入口：外部 Agent 直接提供抽取结果，跳过本地 Qwen3-0.6B。

        契约字段与 _SYSTEM_PROMPT 的 JSON 完全一致，且全部可选：
            memory_type / pad{p,a,d} / confidence / entities / relations / hyperedge
        复用 _from_payload 的 clamp 与白名单校验；source 标为 "agent"，
        让 PAD 轨迹与 eval 能区分「本地模型估的」还是「外部大脑判的」。
        """
        payload = dict(cognition or {})
        # 强容错：外部 Agent 字段类型不合法时收敛为空，绝不让写入链抛异常
        if not isinstance(payload.get("pad"), dict):
            payload["pad"] = {}
        if not isinstance(payload.get("entities"), list):
            payload["entities"] = []
        else:
            payload["entities"] = [x for x in payload["entities"] if isinstance(x, dict)]
        if not isinstance(payload.get("relations"), list):
            payload["relations"] = []
        else:
            payload["relations"] = [x for x in payload["relations"] if isinstance(x, dict)]
        if not isinstance(payload.get("hyperedge"), dict):
            payload["hyperedge"] = {}
        elif not isinstance(payload["hyperedge"].get("members"), list):
            payload["hyperedge"]["members"] = []
        # 外部 Agent 已完成理解，缺省置信度高于 LLM 抽取的 0.6
        payload.setdefault("confidence", 0.9)
        # C6 / Q13：Agent 自报的 confidence 只作参考存 agent_confidence，
        # 门控用 write_router 服务端重算后的值（由 hippocampus 注入 routing 决策）。
        routing = payload.get("routing")
        if isinstance(routing, dict):
            payload["agent_confidence"] = payload.get("confidence", 0.9)
            payload["confidence"] = float(routing.get("confidence", payload["confidence"]))
        extraction = self._from_payload(payload)
        # ★ 零 PAD 兜底已统一下沉到 _from_payload（覆盖 llm/rule/agent 全部来源），
        #   这里只负责把来源如实标为 "agent"。
        return replace(extraction, source="agent")

    def extract(self, content: str, *, agent_id: str = "", use_llm: bool = True) -> Extraction:
        # 批量导入走规则抽取：LLM 单条 0.3~1s，几百条会拖成几分钟，
        # 而导入内容本来就是用户自己的正文，不需要再让模型"理解"一遍。
        if not use_llm:
            return self._rule_extract(content)
        # task_routes["extract"] 配了远程供应商（模型配置页）→ 先走远程；
        # 失败/未配置回落本地 0.6B，规则抽取仍是最后防线。
        route = str((self._config.model_router.task_routes or {}).get("extract") or "")
        if route:
            try:
                from gateway.model_router import get_model_router

                raw = get_model_router().complete_task(
                    "extract", f"Agent={agent_id}\n内容：{content}")
                payload = self._json_from_text(raw)
                return self._from_payload(payload)
            except Exception:  # noqa: BLE001  远程失败回落本地，绝不阻塞登记
                pass
        # llm=None（如 ChatEngine 复用抽取器、不想重复加载 GGUF）时不能调 complete_json，
        # 直接走规则抽取；远程 task_routes["extract"] 已在上方优先尝试过。
        if self._llm is None:
            return self._rule_extract(content)
        try:
            payload = self._llm.complete_json(
                _SYSTEM_PROMPT,
                f"Agent={agent_id}\n内容：{content}",
                required_keys=("memory_type", "pad", "confidence"),
                max_retries=2,
            )
            return self._from_payload(payload)
        except (EngineUnavailable, ValueError, KeyError, TypeError, AttributeError):
            return self._rule_extract(content)

    def extract_multi(self, content: str, *, agent_id: str = "") -> ExtractionBundle:
        """一进多出：一句话 → 一组结构化记忆（profile/fact/emotion/scenario/code）+ 事件骨架。

        来源优先级与 extract() 一致：task_routes["extract"] 远程 → 本地 LLM → 规则拆分。
        任何一步失败都降级到规则拆分（至少出 scenario + emotion 两条），
        保证"一进多出"永不因模型抖动中断写入。
        """
        # ★ 工程硬拦截（第一道边界的代码兜底，先于任何 LLM）：正则命中高频代码特征
        #   或超长连续英文 → 直接判事务性，绝不进认知池/超边/叙事，走 overwrite。
        if self._looks_transactional(content):
            return ExtractionBundle(items=(), transactional=True,
                                    summary=self._transactional_summary(content),
                                    raw_content=content, source="rule")

        raw = ""
        # 1) 远程供应商（用户后续用大模型 LLM，这里优先）
        route = str((self._config.model_router.task_routes or {}).get("extract") or "")
        if route:
            try:
                from gateway.model_router import get_model_router

                raw = get_model_router().complete_task(
                    "extract", f"Agent={agent_id}\n{_MULTI_SYSTEM_PROMPT}\n\n内容：{content}")
            except Exception:  # noqa: BLE001
                raw = ""
        # 2) 本地 LLM
        if not raw and self._llm is not None:
            try:
                raw = self._llm.complete_json(
                    _MULTI_SYSTEM_PROMPT, content,
                    required_keys=("items",), max_retries=1,
                )  # type: ignore[assignment]
                if isinstance(raw, dict):  # complete_json 可能已返回 dict
                    return self._bundle_from_payload(raw)
            except (EngineUnavailable, ValueError, KeyError, TypeError, AttributeError):
                raw = ""
        # 3) 解析远程/本地返回的文本
        if raw:
            try:
                payload = self._json_from_text(str(raw))
                bundle = self._bundle_from_payload(payload)
                if bundle.items:
                    return bundle
            except Exception:  # noqa: BLE001
                pass
        # 4) 规则拆分兜底（不依赖任何模型）
        return self._rule_bundle(content)

    def bundle_from_payload(self, payload: dict[str, Any]) -> ExtractionBundle:
        """★ WorkBuddy 侧前置拆分包 → ExtractionBundle（对外公开契约）。

        与「前置 PAD 注入」同款：对话大模型在 WorkBuddy 侧按 _MULTI_SYSTEM_PROMPT
        拆好 {items,event,complexity,transactional,ephemeral} 后随 save_memory 的
        ``bundle`` 入参提交，后端直接走这里做白名单校验后 remember_bundle，
        跳过 extract_multi（本地 0.6B / 远程抽取模型），零拆分开销。
        """
        return self._bundle_from_payload(payload)

    def _bundle_from_payload(self, payload: dict[str, Any]) -> ExtractionBundle:
        """JSON payload → ExtractionBundle（白名单校验 + 三梯队字段 + 互斥约束）。"""
        transactional = bool(payload.get("transactional", False))
        ephemeral = bool(payload.get("ephemeral", False))
        complexity = str(payload.get("complexity") or "light")
        if complexity not in ("none", "light", "heavy"):
            complexity = "light"
        media = tuple(str(x).strip() for x in (payload.get("media_attachments") or []) if str(x).strip())

        items: list[ExtractionItem] = []
        has_interaction_scenario = False
        for row in (payload.get("items") or []):
            if not isinstance(row, dict):
                continue
            text = str(row.get("content") or "").strip()
            if not text:
                continue
            mt = str(row.get("memory_type", "fact"))
            if mt not in ("fact", "code", "scenario", "emotion", "profile"):
                mt = "fact"
            if self._looks_transactional(text):   # 单条命中代码特征 → 强制 code
                mt = "code"
            rp = row.get("pad") or {}
            pad = PAD(p=_clamp_float(rp.get("p", 0.05)),
                      a=_clamp_float(rp.get("a", 0.10)),
                      d=_clamp_float(rp.get("d", 0.10)))
            if pad.p == 0.0 and pad.a == 0.0 and pad.d == 0.0:
                pad = PAD(p=0.05, a=0.10, d=0.10)
            conf = _clamp01(float(row.get("confidence", 0.6)))
            if mt == "profile":   # 画像单次置信度封顶 0.7，多次出现才由后处理升维
                conf = min(conf, 0.7)
            ents = tuple(
                ExtractedEntity(name=str(e.get("name", "")).strip(),
                                type=str(e.get("type", "concept")))
                for e in (row.get("entities") or []) if isinstance(e, dict) and str(e.get("name", "")).strip()
            )
            needs_edge = mt == "scenario" and bool(row.get("requires_hyperedge", False))
            if needs_edge:
                has_interaction_scenario = True
            items.append(ExtractionItem(memory_type=mt,  # type: ignore[arg-type]
                                        content=text, pad=pad, confidence=conf,
                                        entities=ents, requires_hyperedge=needs_edge))
            if len(items) >= 5:
                break

        items = self._dedupe_profile_vs_fact(items)          # 互斥：偏好只留 profile
        if not any(i.memory_type == "scenario" for i in items):
            items = tuple(i for i in items if i.memory_type != "emotion")  # 无根情绪丢弃

        event = payload.get("event") or {}
        label = str(event.get("label") or event.get("event_summary") or "").strip() or None
        members = tuple(str(m).strip() for m in (event.get("members") or []) if str(m).strip())
        if not has_interaction_scenario or len(members) < 2:  # 超边补全失败→降级散落
            label, members = None, ()

        return ExtractionBundle(items=tuple(items), event_label=label, event_members=members,
                                source="llm", transactional=transactional,
                                summary=str(payload.get("summary") or ""),
                                ephemeral=ephemeral, complexity=complexity,
                                media_attachments=media)

    @staticmethod
    def _dedupe_profile_vs_fact(items: list[ExtractionItem]) -> list[ExtractionItem]:
        """互斥去重：偏好类信息只留 profile，但保留"事实+画像"双抽象层。

        只丢弃那些**明确是偏好复述**的 fact（如 fact="阿鱼喜欢啵啵" + profile 同义），
        不丢弃有独立事实内容的 fact（如 fact="用户换了 Mac M3" + profile="偏好好屏幕"）。
        判据：两条 content 语义高度重合（近乎逐字相同）才算重复；不同抽象层各留一条。
        """
        profiles = [i.content for i in items if i.memory_type == "profile"]
        if not profiles:
            return items
        out: list[ExtractionItem] = []
        for item in items:
            if item.memory_type == "fact":
                # 只有 fact 与某 profile 几乎逐字相同（归一化后相等）才丢弃
                norm = item.content.strip().rstrip("。 ")
                if any(norm == p.strip().rstrip("。 ") for p in profiles):
                    continue
            out.append(item)
        return out

    # 代码硬特征（第一道边界的工程兜底，先于 LLM）
    _CODE_MARKERS = ("def ", "class ", "import ", "from ", "function ", "const ", "let ",
                     "var ", "public ", "private ", "return ", "=>", "{}", "();", "#include", ";")
    _TRANSACT_VERBS = ("改成异步", "写个脚本", "保存这个配置", "重构", "这段 Python", "这段代码")

    @classmethod
    def _looks_transactional(cls, content: str) -> bool:
        """代码/配置/日志硬拦截：高频代码特征 或 超长连续英文 或 事务指令。"""
        text = str(content or "")
        if any(v in text for v in cls._TRANSACT_VERBS):
            return True
        code_hits = sum(1 for m in cls._CODE_MARKERS if m in text)
        if code_hits >= 2:
            return True
        if len(re.findall(r"[A-Za-z_]{200,}", text)) > 0:   # 超长连续英文（日志/代码）
            return True
        return False

    @staticmethod
    def _transactional_summary(content: str) -> str:
        first = str(content or "").strip().splitlines()[0] if str(content or "").strip() else ""
        return first[:40] + ("…" if len(first) > 40 else "")

    def _rule_bundle(self, content: str) -> ExtractionBundle:
        """规则兜底按三梯队分级（信息密度决定粒度，不一律全拆）。"""
        text = content.strip()
        if not text:
            return ExtractionBundle(items=(), ephemeral=True, complexity="none", source="rule")
        # 寒暄/系统指令 → 不进六引擎
        if len(text) <= 8 and any(h in text for h in ("早上好", "晚安", "在吗", "你好", "收到", "清空上下文")):
            return ExtractionBundle(items=(), ephemeral=True, complexity="none", source="rule")
        base = self._rule_extract(text)
        # 交互对象：显式人名/称呼，或"我问/他说/回复/喊我"等双向对话动词
        _PERSON_HINTS = ("宝儿", "啵啵", "阿鱼", "肥鱼", "阿宸", "宸宸", "傻鱼",
                         "我说", "他说", "你问", "我问", "回复", "喊我", "互相", "我们")
        _INTIMACY = ("宝儿", "啵啵", "想你", "爱你", "贴贴", "想我", "喊我", "撒娇")
        has_person = any(e.type == "person" for e in base.entities) or any(
            k in text for k in _PERSON_HINTS)
        has_intimacy = any(k in text for k in _INTIMACY)
        # 双向交互（不是单向倾诉）
        two_way = has_person and any(
            k in text for k in ("你", "他", "她", "我说", "他说", "回复", "吗", "？")) \
            or (has_person and has_intimacy)
        # 情绪：PAD 偏离 或 明确情绪词（气死/烦/开心/崩溃/爱了）
        _EMO_WORDS = ("气死", "烦死", "崩溃", "开心", "爱了", "难过", "委屈", "爽", "糟糕", "太气")
        has_emotion = (abs(base.pad.p) >= 0.2 or base.pad.a >= 0.3
                       or has_intimacy or any(w in text for w in _EMO_WORDS))
        has_profile = any(h in text for h in _PROFILE_HINTS)

        # 重度：双向交互 + (情绪/亲密或偏好) 命中；亲密互动直接判重度（能撑起叙事）
        signals = sum((two_way, has_emotion, has_profile))
        heavy = (two_way and signals >= 2) or (has_intimacy and has_person)

        _strong_emo = any(w in text for w in _EMO_WORDS) or has_intimacy
        items: list[ExtractionItem] = []
        if base.memory_type == "code":
            items = [ExtractionItem("code", text, base.pad, 0.6, base.entities)]
        elif heavy:
            items.append(ExtractionItem("scenario", text, base.pad, 0.5, base.entities,
                                        requires_hyperedge=True))
            if has_emotion:
                items.append(ExtractionItem("emotion", f"这段互动带有情绪色彩：{text[:60]}",
                                            base.pad, 0.45, base.entities))
            if has_profile:
                items.append(ExtractionItem("profile", text, base.pad, 0.55, base.entities))
            if not has_profile:
                items.append(ExtractionItem("fact", text, base.pad, 0.5, base.entities))
        elif _strong_emo and not has_profile:
            # 轻量·明确单向情绪（客户改需求气死我了）：scenario+emotion，单向不建超边
            items = [ExtractionItem("scenario", text, base.pad, 0.45, base.entities),
                     ExtractionItem("emotion", f"情绪：{text[:40]}", base.pad, 0.45, base.entities)]
        elif has_profile:
            # 轻量·客观信息+偏好（换 Mac 屏幕舒服）：两条不同抽象层，content 不逐字相同
            fact_text = re.sub(r"(看着真舒服|真舒服|很舒服|用着爽)", "", text).strip("，。 ,") or text[:40]
            items = [ExtractionItem("fact", fact_text, base.pad, 0.55, base.entities),
                     ExtractionItem("profile", f"偏好：{text}", base.pad, 0.5, base.entities)]
        else:
            items = [ExtractionItem(base.memory_type if base.memory_type != "scenario" else "fact",
                                    text, base.pad, 0.5, base.entities)]
        items = self._dedupe_profile_vs_fact(items)
        if not any(i.memory_type == "scenario" for i in items):
            items = [i for i in items if i.memory_type != "emotion"] or items[:1]
        edge_label = f"互动：{text[:12]}" if heavy else None
        edge_members = ("我", "对方") if edge_label else ()
        return ExtractionBundle(items=tuple(items), source="rule",
                                complexity="heavy" if heavy else "light",
                                event_label=edge_label, event_members=edge_members)

    @staticmethod
    def _json_from_text(raw: str) -> dict[str, Any]:
        """远程模型返回文本 → JSON payload（容忍 ```json 围栏与前后杂文）。"""
        import json as _json

        text = str(raw or "").strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("远程抽取结果不含 JSON 对象")
        payload = _json.loads(text[start:end + 1])
        if not isinstance(payload, dict):
            raise ValueError("远程抽取结果不是对象")
        return payload

    # ── LLM 结果 → Extraction ────────────────────────────────────────────
    def _from_payload(self, payload: dict[str, Any]) -> Extraction:
        raw_pad = payload.get("pad") or {}
        pad = PAD(
            p=_clamp_float(raw_pad.get("p", 0.0)),
            a=_clamp_float(raw_pad.get("a", 0.0)),
            d=_clamp_float(raw_pad.get("d", 0.0)),
        )
        # ★【2026-11-10 缺口1·统一最后防线】_from_payload 是本地0.6B(llm)、
        #   规则(rule)、外部Agent(agent)三条抽取来源的共同出口。任何来源给出
        #   PAD 三轴全 0（模型返回全0 / 调用方漏填 / 显式填零）一律兜中性非零基线，
        #   从抽取出口根治「零 PAD 记忆落库」。17:26 distiller 那条零PAD即 0.6B 返回全0。
        #   不冒充语义判定：仅满足「每条记忆 PAD 非零」不变量。
        if pad.p == 0.0 and pad.a == 0.0 and pad.d == 0.0:
            pad = PAD(p=0.05, a=0.10, d=0.10)
        entities = tuple(
            ExtractedEntity(name=str(item.get("name", "")).strip(), type=str(item.get("type", "concept")))
            for item in payload.get("entities") or []
            if str(item.get("name", "")).strip()
        )
        relations = tuple(
            ExtractedRelation(
                subject=str(item.get("subject", "")).strip(),
                predicate=str(item.get("predicate", "")).strip(),
                object=str(item.get("object", "")).strip(),
            )
            for item in payload.get("relations") or []
            if item.get("subject") and item.get("predicate") and item.get("object")
        )
        hyperedge = payload.get("hyperedge") or {}
        members = tuple(str(m).strip() for m in (hyperedge.get("members") or []) if str(m).strip())
        memory_type = str(payload.get("memory_type", "fact"))
        if memory_type not in ("fact", "code", "scenario", "emotion", "profile"):
            memory_type = "fact"
        return Extraction(
            memory_type=memory_type,  # type: ignore[arg-type]
            pad=pad,
            confidence=_clamp01(float(payload.get("confidence", 0.6))),
            source="llm",
            entities=entities,
            relations=relations,
            hyperedge_label=(str(hyperedge.get("label")) if hyperedge.get("label") else None),
            hyperedge_members=members,
        )

    # ── 规则兜底 ──────────────────────────────────────────────────────────
    def _rule_extract(self, content: str) -> Extraction:
        text = content.strip()
        negative = sum(1 for word in _NEGATIVE if word in text)
        positive = sum(1 for word in _POSITIVE if word in text)
        arousal_hits = sum(1 for word in _HIGH_AROUSAL if word in text)
        calm_hits = sum(1 for word in _LOW_AROUSAL if word in text)

        valence = (positive - negative) / max(positive + negative, 1)
        arousal = (arousal_hits - calm_hits) / max(arousal_hits + calm_hits, 1)
        arousal = max(arousal, 0.4 * arousal_hits / max(arousal_hits + 1, 1))
        dominance = 0.2 if negative else 0.0

        # ★ 中性文本（情绪词典零命中）原本会得到 PAD=(0,0,0)，破坏「每条记忆自带 PAD」
        #   这一全链路不变量。给极小非零基线：p=0.05（微正）、a=0.10（轻微唤醒）、
        #   d=0.10（中性偏可控）。confidence 仍 0.45 低权重，不影响召回排序，
        #   只是保证零 PAD 活跃记忆恒为 0（用户硬指标）。带 cognition 的写入不走这里。
        if positive == 0 and negative == 0 and arousal_hits == 0 and calm_hits == 0:
            valence, arousal, dominance = 0.05, 0.10, 0.10

        return Extraction(
            memory_type=self._rule_memory_type(text),
            pad=PAD(p=_clamp_float(valence), a=_clamp_float(arousal), d=_clamp_float(dominance)),
            confidence=0.45,          # 规则抽取置信度天然低，触发复核/低权重
            source="rule",
            entities=self._rule_entities(text),
        )

    @staticmethod
    def _rule_memory_type(text: str) -> MemoryType:
        if any(hint in text for hint in _PROFILE_HINTS):
            return "profile"
        if any(hint in text for hint in _CODE_HINTS):
            return "code"
        if any(hint in text for hint in _SCENARIO_HINTS):
            return "scenario"
        if any(word in text for word in _NEGATIVE + _POSITIVE):
            return "emotion"
        return "fact"

    @staticmethod
    def _rule_entities(text: str) -> tuple[ExtractedEntity, ...]:
        # 抓英文标识符与书名号/引号内实体；中文实体抽取留给 LLM 通道
        candidates = re.findall(r"[《「\"]([^》」\"]{2,20})[》」\"]", text)
        candidates += re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,30}\b", text)
        unique = list(dict.fromkeys(candidate.strip() for candidate in candidates if candidate.strip()))
        return tuple(ExtractedEntity(name=name, type="concept") for name in unique[:8])

    # ── 组装 Neuron（PAD 已含在内，保证"每条记忆自带 PAD"）──────────────
    def to_neuron(
        self,
        content: str,
        extraction: Extraction,
        *,
        device_id: str,
        agent_id: str,
        namespace: str = "private",
        transaction_from: str = "",
    ) -> Neuron:
        return Neuron(
            device_id=device_id,
            agent_id=agent_id,
            memory_type=extraction.memory_type,
            content=content.strip(),
            pad=extraction.pad,
            confidence=extraction.confidence,
            namespace=namespace,  # type: ignore[arg-type]
            transaction_from=transaction_from,
        )


def _clamp_float(value: Any, bound: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(-bound, min(bound, number))


def _clamp01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, number))
