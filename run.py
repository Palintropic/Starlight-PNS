# run.py — 主运行文件
import os
import sys
from world import get_ena_system, get_mzk_system, SCENES, DEFAULT_SCENE
from router import create_client, judge, API_FORMAT, _get_api_key

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

MODEL         = os.environ.get("MODEL", "mimo-v2.5-pro")
MAX_TURNS     = 8
TEMPERATURE   = 0.85
OOC_THRESHOLD = 5

def call_character(client, character: str, history: list, scene: dict, correction: str = None) -> str:
    if character == "ena":
        system = get_ena_system(scene)
        char_name = "绘名"
    else:
        system = get_mzk_system(scene)
        char_name = "瑞希"

    if correction:
        system += f"\n\n【注意】{correction}"

    print(f"[{char_name}] 生成中...")

    if API_FORMAT == "openai":
        oai_history = [{"role": "system", "content": system}] + history
        response = client.chat.completions.create(
            model=MODEL, max_tokens=200, temperature=TEMPERATURE,
            messages=oai_history,
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError(f"API返回空内容，finish_reason: {response.choices[0].finish_reason}")
        return content.strip()
    else:
        response = client.messages.create(
            model=MODEL, max_tokens=200, temperature=TEMPERATURE,
            system=system, messages=history,
        )
        return response.content[0].text.strip()


def run():
    print("=" * 60)
    print("  PNS — Project Nightcord Sanctuary  v0.1")
    print("=" * 60)
    scene = SCENES[DEFAULT_SCENE]
    print(f"\n{scene['trigger']}")
    print(f"时间：{scene['time']} | 地点：{scene['location']} | 轮次上限：{MAX_TURNS}")
    print("─" * 60)

    # 加载API key
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    api_key = _get_api_key()
    if not api_key:
        print("\n❌ 找不到API Key，请先运行：python oobe.py")
        sys.exit(1)

    client = create_client(api_key)

    # 对话历史（两个角色共用同一段历史）
    history = [{"role": "user", "content": f"【场景】{scene['trigger']}\n请开始对话。"}]

    stats = {"ooc_count": 0, "scores": [], "corrections": 0}

    # 瑞希先开口
    current = "mzk"
    correction_next = None

    print("\n【对话开始】\n")

    for turn in range(1, MAX_TURNS + 1):
        char_name = "瑞希" if current == "mzk" else "绘名"

        try:
            reply = call_character(client, current, history, scene, correction_next)
        except Exception as e:
            print(f"\n❌ 角色调用失败: {e}")
            break

        print(f"\n第{turn}轮 | {char_name}：{reply}")

        # 加入历史（交替role）
        role = "assistant" if len(history) % 2 == 1 else "user"
        history.append({"role": role, "content": f"{char_name}：{reply}"})

        # Router判断
        result = judge(client, current, reply, turn)
        score = result.get("score", 0)
        is_ooc = result.get("is_ooc", False)

        stats["scores"].append(score)
        if is_ooc:
            stats["ooc_count"] += 1
            correction_next = result.get("correction")
            if correction_next:
                stats["corrections"] += 1
        else:
            correction_next = None

        print("─" * 40)

        # 切换角色
        current = "ena" if current == "mzk" else "mzk"

    # 统计
    print("\n" + "=" * 60)
    print("  统计数据")
    print("=" * 60)
    print(f"总轮次：{len(stats['scores'])}")
    print(f"OOC次数：{stats['ooc_count']}")
    print(f"Router介入：{stats['corrections']}次")
    if stats["scores"]:
        print(f"平均漂移分数：{sum(stats['scores'])/len(stats['scores']):.2f}/10")
        print(f"最高漂移分数：{max(stats['scores'])}/10")
    print("=" * 60)


if __name__ == "__main__":
    run()
