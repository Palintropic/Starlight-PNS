# router.py — Router判断逻辑（Anthropic格式）
import json
import anthropic
from world import ROUTER_SYSTEM

def create_client(api_key: str):
    return anthropic.Anthropic(
        api_key=api_key,
        base_url="https://api.xiaomimimo.com/anthropic"
    )

def judge(client, character: str, message: str, turn: int) -> dict:
    char_name = "绘名" if character == "ena" else "瑞希"
    prompt = f"第{turn}轮，{char_name}说：「{message}」\n\n请判断是否OOC。"

    print(f"\n[Router] 判断第{turn}轮 {char_name}...")

    try:
        response = client.messages.create(
            model="mimo-v2.5-pro",
            max_tokens=300,
            temperature=0.1,
            system=ROUTER_SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )

        raw = response.content[0].text.strip()

        # 清理markdown
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        result = json.loads(raw)

        score = result.get("score", 0)
        is_ooc = result.get("is_ooc", False)
        reason = result.get("reason", "")

        status = "🔴 OOC" if is_ooc else "🟢 正常"
        print(f"[Router] {status} | 分数: {score}/10 | {reason}")

        if is_ooc:
            print(f"[Router] 纠正提示: {result.get('correction', '')}")

        return result

    except json.JSONDecodeError as e:
        print(f"[Router] ⚠️ JSON解析失败: {e}\n原始: {raw}")
        return {"character": character, "score": 0, "reason": "解析失败", "is_ooc": False, "correction": None}
    except Exception as e:
        print(f"[Router] ❌ 调用失败: {e}")
        return {"character": character, "score": 0, "reason": str(e), "is_ooc": False, "correction": None}
