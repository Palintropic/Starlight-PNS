# run.py — 主运行文件
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from world import get_ena_system, get_mzk_system, get_ena_system_compat, get_mzk_system_compat, SCENES, DEFAULT_SCENE
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
MAX_TOKENS    = int(os.environ.get("MAX_TOKENS", "1024"))
API_DELAY     = float(os.environ.get("API_DELAY", "3"))

def _strip_prefix(text: str, char_name: str) -> str:
    prefix = char_name + "："
    while text.startswith(prefix):
        text = text[len(prefix):]
    return text

def call_character(client, character: str, history: list, scene: dict, correction: str = None) -> str:
    use_compat = "flash-lite" in MODEL.lower()
    if character == "ena":
        system = get_ena_system_compat(scene) if use_compat else get_ena_system(scene)
        char_name = "绘名"
    else:
        system = get_mzk_system_compat(scene) if use_compat else get_mzk_system(scene)
        char_name = "瑞希"

    if correction:
        system += f"\n\n【注意】{correction}"

    print(f"[{char_name}] 生成中...")

    if API_FORMAT == "openai":
        oai_history = [{"role": "system", "content": system}] + history
        response = client.chat.completions.create(
            model=MODEL, max_tokens=MAX_TOKENS, temperature=TEMPERATURE,
            messages=oai_history,
        )
        content = response.choices[0].message.content
        if not content:
            choice = response.choices[0]
            print(f"[DEBUG] finish_reason: {choice.finish_reason}")
            print(f"[DEBUG] full response: {response.model_dump()}")
            raise ValueError(f"API返回空内容，finish_reason: {choice.finish_reason}")
        return _strip_prefix(content.strip(), char_name)
    else:
        response = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, temperature=TEMPERATURE,
            system=system, messages=history,
        )
        return _strip_prefix(response.content[0].text.strip(), char_name)


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
    turn_log = []

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

        turn_log.append({
            "turn": turn, "char_name": char_name, "reply": reply,
            "score": score, "is_ooc": is_ooc,
            "drift_type": result.get("drift_type", ""),
            "reason": result.get("reason", ""),
            "correction": result.get("correction"),
            "needs_human_review": result.get("needs_human_review", False),
        })

        print("─" * 40)
        time.sleep(API_DELAY)

        # 切换角色
        current = "ena" if current == "mzk" else "mzk"

    # 统计
    print("\n" + "=" * 60)
    print("  统计数据")
    print("=" * 60)
    print(f"总轮次：{len(stats['scores'])}")
    print(f"OOC次数：{stats['ooc_count']}")
    print(f"Router介入：{stats['corrections']}次")
    avg_score = sum(stats["scores"]) / len(stats["scores"]) if stats["scores"] else 0
    if stats["scores"]:
        print(f"平均漂移分数：{avg_score:.2f}/10")
        print(f"最高漂移分数：{max(stats['scores'])}/10")
    print("=" * 60)

    if turn_log:
        final_stats = {
            "total_turns": len(stats["scores"]),
            "ooc_count": stats["ooc_count"],
            "corrections": stats["corrections"],
            "avg_score": round(avg_score, 2),
            "max_score": max(stats["scores"]) if stats["scores"] else 0,
        }
        try:
            saved = _save_history(scene, MODEL, turn_log, final_stats)
            print(f"\n📄 历史记录已保存：{saved}")
        except Exception as e:
            print(f"\n⚠ 历史记录保存失败：{e}")


def _save_history(scene: dict, model: str, turns: list, stats: dict) -> Path:
    history_dir = Path("history")
    history_dir.mkdir(exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = history_dir / f"{ts}_{scene['id']}.md"

    lines = []
    lines.append(f"# {scene['label']}")
    lines.append(f"")
    lines.append(f"> {scene['trigger']}")
    lines.append(f"")
    lines.append(f"| | |")
    lines.append(f"|---|---|")
    lines.append(f"| 时间 | {scene['time']} |")
    lines.append(f"| 地点 | {scene['location']} |")
    lines.append(f"| 模型 | {model} |")
    lines.append(f"| 生成时间 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |")
    lines.append(f"")
    lines.append(f"---")
    lines.append(f"")

    for t in turns:
        score = t["score"]
        score_icon = "🟢" if score <= 2 else ("🟡" if score <= 5 else "🔴")

        lines.append(f"**第 {t['turn']} 轮 · {t['char_name']}**")
        lines.append(f"")
        lines.append(t["reply"])
        lines.append(f"")

        router_line = f"{score_icon} Router {score}/10 · {t.get('drift_type', '')}"
        if t.get("reason"):
            router_line += f" — {t['reason']}"
        if t.get("needs_human_review"):
            router_line += " ⚑待人工校验"
        lines.append(f"<sub>{router_line}</sub>")

        if t.get("correction"):
            lines.append(f"")
            lines.append(f"> ⚡ **纠正** {t['correction']}")

        lines.append(f"")
        lines.append(f"---")
        lines.append(f"")

    lines.append(f"## 统计")
    lines.append(f"")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|---|---|")
    lines.append(f"| 总轮次 | {stats['total_turns']} |")
    lines.append(f"| OOC次数 | {stats['ooc_count']} |")
    lines.append(f"| Router介入 | {stats['corrections']} 次 |")
    lines.append(f"| 平均漂移分数 | {stats['avg_score']}/10 |")
    lines.append(f"| 最高漂移分数 | {stats['max_score']}/10 |")

    filename.write_text("\n".join(lines), encoding="utf-8")
    return filename


if __name__ == "__main__":
    run()
