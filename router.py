# router.py — Router判断逻辑
import json
import os
from world import ROUTER_SYSTEM

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_FORMAT = os.environ.get("API_FORMAT", "anthropic")
BASE_URL    = os.environ.get("BASE_URL", "https://token-plan-cn.xiaomimimo.com")
_KEY_NAME   = os.environ.get("PNS_API_KEY_NAME", "MIMO_API_KEY")

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
            max_tokens=300,
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
            max_tokens=300,
            temperature=0.1,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        return resp.content[0].text.strip()

def judge(client, character: str, message: str, turn: int) -> dict:
    model = os.environ.get("MODEL", "mimo-v2.5-pro")
    char_name = "绘名" if character == "ena" else "瑞希"
    prompt = f"第{turn}轮，{char_name}说：「{message}」\n\n请判断是否OOC。"

    print(f"\n[Router] 判断第{turn}轮 {char_name}...")

    try:
        raw = _call(client, model, ROUTER_SYSTEM, prompt)

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
