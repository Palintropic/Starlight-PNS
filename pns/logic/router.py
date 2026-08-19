# pns/logic/router.py
import json
import os

API_FORMAT  = os.environ.get("API_FORMAT", "anthropic")
BASE_URL    = os.environ.get("BASE_URL", "https://api.xiaomimimo.com/anthropic")
_KEY_NAME   = os.environ.get("PNS_API_KEY_NAME", "MIMO_API_KEY")
OOC_THRESHOLD = float(os.environ.get("OOC_THRESHOLD", "5"))
METHODOLOGY_VERSION = "v3_contextual_multidimensional"

DIMENSION_KEYS = (
    "character_facts",
    "psychological_mechanism",
    "language_structure",
    "media_authenticity",
    "task_compliance",
    "unsupported_invention",
    "timeline_boundary",
)


def _get_api_key() -> str:
    return os.environ.get(_KEY_NAME, "")


def create_client(api_key: str = None):
    key = api_key or _get_api_key()
    if API_FORMAT == "openai":
        from openai import OpenAI
        return OpenAI(api_key=key, base_url=BASE_URL)
    else:
        import anthropic
        return anthropic.Anthropic(api_key=key, base_url=BASE_URL)


def extract_anthropic_text(response) -> str:
    """从Anthropic兼容响应中提取全部文本块，跳过MiMo等模型的thinking块。"""
    texts = []
    for block in getattr(response, "content", []):
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    if not texts:
        block_types = [type(block).__name__ for block in getattr(response, "content", [])]
        raise ValueError(f"API返回中没有文本块，content类型: {block_types}")
    return "\n".join(texts).strip()


def _call(client, model: str, system: str, user_msg: str) -> str:
    if API_FORMAT == "openai":
        resp = client.chat.completions.create(
            model=model,
            max_tokens=1024,
            temperature=0.1,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user_msg},
            ],
        )
        return resp.choices[0].message.content.strip()
    else:
        request = dict(
            model=model,
            max_tokens=1024,
            temperature=0.1,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        # MiMo 2.5 may spend the entire Router output budget on a ThinkingBlock,
        # leaving no JSON to parse. Router is a constrained classification call,
        # so disable extended thinking here while keeping it available to normal
        # character-generation requests.
        if "xiaomimimo.com" in BASE_URL:
            request["thinking"] = {"type": "disabled"}
        resp = client.messages.create(**request)
        return extract_anthropic_text(resp)


ROUTER_SYSTEM = """
你是PNS世界的监督者Router，负责判断角色是否发生了角色漂移（OOC）。

【判断原则（重要）】
OOC判断分为两个独立层次，必须分别评估：

层次1：结构/语气层（你可以判断）
- 语言密度是否符合角色习惯
- 是否出现结构性漂移或助手化偏移

层次2：内容具体性层（你可能判断不准）
- 内容是否真的贴近角色的具体表达习惯
- 这层需要对PJSK剧情的真实积累，你可能被"形式对了"骗过
- 当你不确定时，在reason里注明"内容具体性待人工校验"

【两种助手化漂移的区分】
类型A：角色语气层面的助手化
→ 角色说话本身像在给建议、留选择权
→ 纠正方式：调整语气和说话结构

类型B：任务执行层面的助手化
→ 被要求"写一段角色的台词"时，
  本能地把决定权交还给用户（"发不发随你"）
  而不是直接产出完整台词
→ 纠正方式：明确要求直接产出，不留选择给用户

【媒介真实性判断】
判断输出是"被写出来的对话"还是"被打出来的对话"：

被写出来的（OOC信号）：
- 书面化的拟声词表演兴奋（啊哈哈哈哈）
- 句子之间有逻辑衔接，像在叙述一个场景
- 每句话都用感叹号收尾，情绪强度统一

被打出来的（正常）：
- 短促、跳跃、允许语法不完整
- 情绪靠句子破碎程度本身体现
- 一次兴奋的反应，内容压缩进一两句话里

【context dilution警告】
对话超过7轮后，前期纠正效果容易失效
此时需要重新完整注入纠正指令，不能只做局部修正

【漂移评分】
0-2: 完全in-character，无异常
3-4: 轻微偏移，语气略有不符
5-6: 明显OOC，需要correction
7-8: 严重漂移，角色核心特征丢失
9-10: 角色崩塌

注意：结构性漂移即使内容100%符合性格，也单独触发3-4分

【精细验收维度】
你必须分别评估以下七个维度，不能让“内容很像角色”掩盖其他问题：
1. character_facts：身份、关系、既有事实是否正确
2. psychological_mechanism：动机、触发条件与应对路径是否符合角色
3. language_structure：句式、密度、停顿和表达收放是否符合角色与场景
4. media_authenticity：是否像即时对话，而非完整书面稿、总结稿或会议纪要
5. task_compliance：是否遵守原始任务，例如“只输出下一句话”、指定语言或格式
6. unsupported_invention：是否擅自补出原输入与既有资料未提供的具体事实
7. timeline_boundary：是否越过当前时间线或把未发生内容写成既成事实

每个维度均给0-10分和一句理由。总drift_score不得低于任一维度最高分；
任一维度达到5分即视为OOC。若缺少判断某维度所需的上下文，要标记人工复核，
不得用其他维度的低分抵消。

【输出格式（只输出JSON，不要其他内容）】
{
  "character": "角色id",
  "drift_score": 数字(0-10),
  "dimensions": {
    "character_facts": {"score": 数字(0-10), "reason": "一句话原因"},
    "psychological_mechanism": {"score": 数字(0-10), "reason": "一句话原因"},
    "language_structure": {"score": 数字(0-10), "reason": "一句话原因"},
    "media_authenticity": {"score": 数字(0-10), "reason": "一句话原因"},
    "task_compliance": {"score": 数字(0-10), "reason": "一句话原因"},
    "unsupported_invention": {"score": 数字(0-10), "reason": "一句话原因"},
    "timeline_boundary": {"score": 数字(0-10), "reason": "一句话原因"}
  },
  "confidence": 数字(0-1),
  "drift_type": "无 / 内容OOC / 结构性漂移 / 媒介失真 / 助手化A / 助手化B",
  "reason": "一句话原因",
  "needs_human_review": true或false,
  "correction": "纠正提示或null"
}
""".strip()


def _build_router_system(character: str) -> tuple[str, str]:
    """ROUTER_SYSTEM（通用判断框架）+ 角色专属 router_reference（评分层，动态读取，若角色尚未提供则跳过）

    返回 (system_prompt, router_reference_status)。status 为 "loaded" 或 "generic_fallback"，
    随判分结果一起写入 drift_scores.jsonl，用于事后区分退化判断产出的数据。
    """
    from pns.world.characters.registry import get_character_router_reference
    router_reference = get_character_router_reference(character)
    if router_reference is None:
        print(f"[Router] ⚠️ 角色 {character} 暂无 router_reference 文件，本次仅用通用框架评分，无角色专属判据")
        return ROUTER_SYSTEM, "generic_fallback"
    return f"{ROUTER_SYSTEM}\n\n【角色专属评分参考：{character}】\n{router_reference}", "loaded"


def _bounded_score(value, default: float = 0.0) -> float:
    try:
        return max(0.0, min(10.0, float(value)))
    except (TypeError, ValueError):
        return default


def _normalize_dimensions(raw_dimensions) -> tuple[dict, bool]:
    """规范七维评分；返回 (dimensions, 是否完整)。"""
    source = raw_dimensions if isinstance(raw_dimensions, dict) else {}
    normalized = {}
    complete = True
    for key in DIMENSION_KEYS:
        item = source.get(key)
        if not isinstance(item, dict) or "score" not in item:
            complete = False
            item = {}
        normalized[key] = {
            "score": _bounded_score(item.get("score")),
            "reason": str(item.get("reason", "")).strip(),
        }
    return normalized, complete


def _format_recent_history(history: list | None, limit: int = 8) -> str:
    if not history:
        return "（无）"
    compact = []
    for item in history[-limit:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "unknown"))
        content = str(item.get("content", ""))
        compact.append({"role": role, "content": content[:2000]})
    return json.dumps(compact, ensure_ascii=False)


def judge(
    client,
    character: str,
    message: str,
    turn: int,
    scene: dict | None = None,
    original_request: str | None = None,
    recent_history: list | None = None,
    correction_applied: str | None = None,
) -> dict:
    model = os.environ.get("EVALUATOR_MODEL") or os.environ.get("MODEL", "mimo-v2.5-pro")
    evaluator_provider = os.environ.get("PROVIDER", "")
    from pns.world.characters.registry import get_character_metadata
    from pns.world.scenes import LORE_TIER_LABELS, LORE_TIER_INFERRED, LORE_TIER_UNVERIFIED, LORE_TIER_CANON
    char_name = get_character_metadata(character)['name']
    router_system, router_reference_status = _build_router_system(character)

    lore_context = ""
    if scene is not None:
        tier = scene.get("lore_tag", LORE_TIER_CANON)
        tier_label = LORE_TIER_LABELS.get(tier, tier)
        lore_context = f"\n【当前场景世界观确定性】{tier_label}（{scene.get('label', '')}）\n"
        if tier == LORE_TIER_INFERRED:
            lore_context += (
                "注意：此场景基于逻辑推断构建，官方未明确描写。"
                "判断OOC时不应以「官方是否有对应描写」作为唯一标准，"
                "而应评估角色的行为逻辑、语气是否与已知人设自洽。\n"
            )
        elif tier == LORE_TIER_UNVERIFIED:
            lore_context += (
                "注意：此场景设定尚未验证，评估时请主动降低confidence，"
                "并在drift_type模糊时优先标记needs_human_review。\n"
            )

    prompt = (
        f"{lore_context}【原始任务/当前直接要求】\n{original_request or '（未提供）'}\n\n"
        f"【生成前最近对话历史】\n{_format_recent_history(recent_history)}\n\n"
        f"【本轮是否注入过纠正】\n{correction_applied or '（无）'}\n\n"
        f"【待验收输出】\n第{turn}轮，{char_name}说：「{message}」\n\n"
        "请按七个维度判断是否OOC或违反任务，并只输出指定JSON。"
    )

    try:
        raw = _call(client, model, router_system, prompt)

        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        result = json.loads(raw)

        dimensions, dimensions_complete = _normalize_dimensions(result.get("dimensions"))
        dimension_max = max(item["score"] for item in dimensions.values())
        drift_score = max(_bounded_score(result.get("drift_score")), dimension_max)
        result["drift_score"] = drift_score
        result["dimensions"] = dimensions
        result["dimensions_complete"] = dimensions_complete
        result["confidence"] = float(result.get("confidence", 0.5))
        result["is_ooc"] = drift_score >= OOC_THRESHOLD
        if not dimensions_complete:
            result["needs_human_review"] = True
        result.setdefault("character", character)
        result["scene_id"] = scene.get("id", "") if scene else ""
        result["lore_tag"] = scene.get("lore_tag", "") if scene else ""
        result["router_reference_status"] = router_reference_status
        result["methodology_version"] = METHODOLOGY_VERSION
        result["evaluator_model"] = model
        result["evaluator_provider"] = evaluator_provider
        return result

    except json.JSONDecodeError as e:
        print(f"[Router] ⚠️ JSON解析失败: {e}\n原始: {raw}")
        return {
            "character": character, "drift_score": 0, "confidence": 0.0,
            "drift_type": "解析失败", "reason": "解析失败", "is_ooc": False,
            "needs_human_review": True, "correction": None,
            "dimensions": _normalize_dimensions(None)[0],
            "dimensions_complete": False,
            "methodology_version": METHODOLOGY_VERSION,
            "router_reference_status": router_reference_status,
            "evaluator_model": model,
            "evaluator_provider": evaluator_provider,
        }
    except Exception as e:
        print(f"[Router] ❌ 调用失败: {e}")
        return {
            "character": character, "drift_score": 0, "confidence": 0.0,
            "drift_type": "error", "reason": str(e), "is_ooc": False,
            "needs_human_review": True, "correction": None,
            "dimensions": _normalize_dimensions(None)[0],
            "dimensions_complete": False,
            "methodology_version": METHODOLOGY_VERSION,
            "router_reference_status": router_reference_status,
            "evaluator_model": model,
            "evaluator_provider": evaluator_provider,
        }
