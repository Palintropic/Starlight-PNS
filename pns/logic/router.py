# pns/logic/router.py
import json
import os

API_FORMAT  = os.environ.get("API_FORMAT", "anthropic")
BASE_URL    = os.environ.get("BASE_URL", "https://token-plan-cn.xiaomimimo.com/anthropic")
_KEY_NAME   = os.environ.get("PNS_API_KEY_NAME", "MIMO_API_KEY")
OOC_THRESHOLD = float(os.environ.get("OOC_THRESHOLD", "5"))


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
        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            temperature=0.1,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        return resp.content[0].text.strip()


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

【输出格式（只输出JSON，不要其他内容）】
{
  "character": "角色id",
  "drift_score": 数字(0-10),
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


def judge(client, character: str, message: str, turn: int, scene: dict | None = None) -> dict:
    model = os.environ.get("MODEL", "mimo-v2.5-pro")
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

    prompt = f"{lore_context}第{turn}轮，{char_name}说：「{message}」\n\n请判断是否OOC。"

    try:
        raw = _call(client, model, router_system, prompt)

        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        result = json.loads(raw)

        drift_score = float(result.get("drift_score", 0))
        result["drift_score"] = drift_score
        result["confidence"] = float(result.get("confidence", 0.5))
        result["is_ooc"] = drift_score >= OOC_THRESHOLD
        result.setdefault("character", character)
        result["scene_id"] = scene.get("id", "") if scene else ""
        result["lore_tag"] = scene.get("lore_tag", "") if scene else ""
        result["router_reference_status"] = router_reference_status
        return result

    except json.JSONDecodeError as e:
        print(f"[Router] ⚠️ JSON解析失败: {e}\n原始: {raw}")
        return {
            "character": character, "drift_score": 0, "confidence": 0.0,
            "drift_type": "解析失败", "reason": "解析失败", "is_ooc": False,
            "needs_human_review": True, "correction": None,
            "router_reference_status": router_reference_status,
        }
    except Exception as e:
        print(f"[Router] ❌ 调用失败: {e}")
        return {
            "character": character, "drift_score": 0, "confidence": 0.0,
            "drift_type": "error", "reason": str(e), "is_ooc": False,
            "needs_human_review": True, "correction": None,
            "router_reference_status": router_reference_status,
        }
