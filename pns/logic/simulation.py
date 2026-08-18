# pns/logic/simulation.py — 角色调用 / Router 判分 / 归档写盘
# 纯业务逻辑，不涉及 WebSocket 或任何具体传输层；调用方（目前是
# pns.interfaces.simulate 的 /ws/run）只负责把这里的结果转成协议消息。
import asyncio
from datetime import datetime
from pathlib import Path

from pns.world import get_character_system
from pns.world.characters import registry as character_registry
import pns.logic.router as router_mod


def _strip_prefix(text: str, char_name: str) -> str:
    prefix = char_name + "："
    while text.startswith(prefix):
        text = text[len(prefix):]
    return text


async def call_character_async(
    client, character: str, history: list, scene: dict, model: str,
    max_tokens: int, temperature: float, correction: str = None,
) -> str:
    use_compat = "flash-lite" in model.lower()
    try:
        system = get_character_system(character, scene, compat=use_compat)
    except ValueError as e:
        # 角色存在于 pack 但还没有 prompt（not_ready/partial 且未补内容）
        raise character_registry.CharacterNotReadyError(character, str(e)) from e

    meta = character_registry.get_character_metadata(character)
    char_name = meta.get("name", character)

    if correction:
        system += f"\n\n【注意】{correction}"

    loop = asyncio.get_event_loop()

    def _call():
        if router_mod.API_FORMAT == "openai":
            oai_history = [{"role": "system", "content": system}] + history
            response = client.chat.completions.create(
                model=model, max_tokens=max_tokens, temperature=temperature,
                messages=oai_history,
            )
            content = response.choices[0].message.content
            if not content:
                raise ValueError(f"API返回空内容，finish_reason: {response.choices[0].finish_reason}")
            return _strip_prefix(content.strip(), char_name)
        else:
            response = client.messages.create(
                model=model, max_tokens=max_tokens, temperature=temperature,
                system=system, messages=history,
            )
            return _strip_prefix(router_mod.extract_anthropic_text(response), char_name)

    return await loop.run_in_executor(None, _call)


async def judge_async(
    client,
    character: str,
    message: str,
    turn: int,
    scene: dict | None = None,
    original_request: str | None = None,
    recent_history: list | None = None,
    correction_applied: str | None = None,
) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: router_mod.judge(
            client,
            character,
            message,
            turn,
            scene,
            original_request=original_request,
            recent_history=recent_history,
            correction_applied=correction_applied,
        ),
    )


def save_history(history_dir: Path, session_id: str, scene: dict, model: str, turns: list, stats: dict) -> Path:
    history_dir.mkdir(exist_ok=True)

    filename = history_dir / f"{session_id}.md"

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
        char_name = t["char_name"]
        reply     = t["reply"]
        score     = t["score"]
        is_ooc    = t["is_ooc"]
        drift     = t.get("drift_type", "")
        reason    = t.get("reason", "")
        correction = t.get("correction")
        review    = t.get("needs_human_review", False)

        score_icon = "🟢" if score <= 2 else ("🟡" if score <= 5 else "🔴")

        lines.append(f"**第 {t['turn']} 轮 · {char_name}**")
        lines.append(f"")
        lines.append(reply)
        lines.append(f"")

        router_line = f"{score_icon} Router {score}/10 · {drift}"
        if reason:
            router_line += f" — {reason}"
        if review:
            router_line += " ⚑待人工校验"
        lines.append(f"<sub>{router_line}</sub>")

        if correction:
            lines.append(f"")
            lines.append(f"> ⚡ **纠正** {correction}")

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
