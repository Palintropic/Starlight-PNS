# pns/logic/simulation.py — 角色调用 / Router 判分 / 归档写盘
# 纯业务逻辑，不涉及 WebSocket 或任何具体传输层；调用方是
# pns.runtime.session_runtime.SessionRuntime，它负责编排，
# 结果最终由 pns.interfaces.simulate 转成协议消息发给客户端。
import asyncio
import json
from datetime import datetime
from pathlib import Path

from pns.world import get_character_system
from pns.world.context import render_clock, render_session_location
from pns.world.characters import registry as character_registry
import pns.logic.router as router_mod


class GenerationTruncated(ValueError):
    """模型在说完之前撞到了 max_tokens。

    它单独成一类，是因为后果跟"调用失败"完全不同：调用失败什么都没拿到，
    而这一档**拿到了半句话**——而且那半句话看起来跟一句完整的话一模一样。
    把它当成一句台词提交，就是让角色说了一句它没说完的话，然后这句话变成
    世界真相、被别人观察到、被记进记忆。所以这里响亮失败，不截断也不将就
    （跟 parse_line() 拒绝超长输出是同一条规矩）。
    """


def _strip_prefix(text: str, char_name: str) -> str:
    prefix = char_name + "："
    while text.startswith(prefix):
        text = text[len(prefix):]
    return text


def call_character(
    client, character: str, history: list, context, model: str,
    max_tokens: int, temperature: float, correction: str = None,
    *, registry=None,
) -> str:
    """调用角色模型（同步）。

    context 是权威的 WorldState（新路径）、角色作用域的世界投影（自主路径），
    或遗留 scene dict（兼容路径）；三种都只被渲染成提示词文本，不反向影响
    世界状态。

    registry 是本次会话锁定的 ContentRegistry 快照：提示词文本和 provider 设定
    都从它取，所以会话跑到一半有人重载配置，这一路调用不会串到新配置上去。
    传 None 走遗留路径（直接读磁盘上的角色包），只给还没迁移的调用方用。

    这是**唯一**一处按 provider 分支调用生成模型的地方：研究会话走
    call_character_async()，自主路径由 pns/interfaces/composition.py 接给
    pns/runtime/autonomy/prompt.py 的生成适配器 —— 两条路进来的都是这个函数。
    多写一份分支就会出现"一条路修好了、另一条还在用旧写法"。
    """
    use_compat = "flash-lite" in model.lower()
    if registry is not None:
        system = registry.character_system(character, context, compat=use_compat)
        char_name = registry.character_name(character)
    else:
        try:
            system = get_character_system(character, context, compat=use_compat)
        except ValueError as e:
            # 角色存在于 pack 但还没有 prompt（not_ready/partial 且未补内容）
            raise character_registry.CharacterNotReadyError(character, str(e)) from e
        meta = character_registry.get_character_metadata(character)
        char_name = meta.get("name", character)

    if correction:
        system += f"\n\n【注意】{correction}"

    api_format = registry.models.api_format if registry is not None else router_mod.API_FORMAT

    if api_format == "openai":
        oai_history = [{"role": "system", "content": system}] + history
        response = client.chat.completions.create(
            model=model, max_tokens=max_tokens, temperature=temperature,
            messages=oai_history,
        )
        choice = response.choices[0]
        content = choice.message.content
        if not content:
            raise ValueError(f"API返回空内容，finish_reason: {choice.finish_reason}")
        if getattr(choice, "finish_reason", None) == "length":
            raise GenerationTruncated(
                f"模型在 max_tokens={max_tokens} 处被截断，这一句没说完"
            )
        return _strip_prefix(content.strip(), char_name)
    else:
        response = client.messages.create(
            model=model, max_tokens=max_tokens, temperature=temperature,
            system=system, messages=history,
        )
        text = _strip_prefix(router_mod.extract_anthropic_text(response), char_name)
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise GenerationTruncated(
                f"模型在 max_tokens={max_tokens} 处被截断，这一句没说完"
            )
        return text


async def call_character_async(
    client, character: str, history: list, context, model: str,
    max_tokens: int, temperature: float, correction: str = None,
    *, registry=None,
) -> str:
    """call_character() 的异步包装：模型调用挪到线程池，事件循环不被阻塞。"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: call_character(
            client, character, history, context, model,
            max_tokens, temperature, correction, registry=registry,
        ),
    )


async def judge_async(
    client,
    character: str,
    message: str,
    turn: int,
    scene: dict | None = None,
    original_request: str | None = None,
    recent_history: list | None = None,
    correction_applied: str | None = None,
    registry=None,
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
            registry=registry,
        ),
    )


def append_drift_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def save_history(
    history_dir: Path,
    session_id: str,
    scene: dict,
    model: str,
    turns: list,
    stats: dict,
    world=None,
) -> Path:
    """把一次会话写成人类可读的 Markdown 投影。

    标题和开场白仍来自遗留 scene（它就是这份记录的叙事来源），但时间/地点
    在传入 world 时改从权威 WorldState 投影 —— 归档不再自己留一份世界状态。
    """
    history_dir.mkdir(parents=True, exist_ok=True)

    filename = history_dir / f"{session_id}.md"

    lines = []
    lines.append(f"# {scene['label']}")
    lines.append(f"")
    lines.append(f"> {scene['trigger']}")
    lines.append(f"")
    lines.append(f"| | |")
    lines.append(f"|---|---|")
    if world is not None:
        world_time = render_clock(world.clock)
        world_place = render_session_location(world)
    else:
        world_time, world_place = scene["time"], scene["location"]
    lines.append(f"| 时间 | {world_time} |")
    lines.append(f"| 地点 | {world_place} |")
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
