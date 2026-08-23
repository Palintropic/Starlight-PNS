# pns/runtime/memory/projection.py — 召回结果 → 提示词里的那几行
#
# 这一层唯一的职责是**删减**：把一次召回砍成"这个角色此刻能说出口的那部分"。
# 所以它是白名单式的 —— 每一类记忆显式声明它渲染成什么样，内容里没被点名的
# 键一个都不出现。黑名单迟早会漏。
#
# 四类东西永远不进提示投影：
#
#   记忆 / 事件 ID      角色不会想起"这是第 3 号事件"。它们是系统簿记。
#   曝光理由码          "我因为是频道成员所以听见了"不是一段回忆。
#   显著度 / 得分       编码与召回策略的内部标量，不是经验。
#   provenance          哪个编码器写的、走的哪条感知通道 —— 系统过程不等于
#                       角色经验（架构文档 §15）。
#
# 别人的记忆更不可能出现在这里：召回本身就只接受单个角色的记忆，这一层拿到的
# 就已经是收窄过的结果。
from typing import Dict, List, Mapping, Tuple

from pns.models.memory import MemoryClass, MemoryRecord
from pns.runtime.memory.recall import RecallResult

PROMPT_TITLE = "【想起来的事】"

# 每一类记忆在提示词里的标签。闭集：没登记的类别渲染不出来（也不该存在 ——
# 有测试盯着类别与这张表一一对应）。
_TAGS: Dict[MemoryClass, str] = {
    MemoryClass.COMMITMENT: "约定",
    MemoryClass.IDENTITY: "关于我",
    MemoryClass.RELATIONAL: "相处",
    MemoryClass.SEMANTIC: "知道",
    MemoryClass.EPISODIC: "记得",
    MemoryClass.WORKING: "刚才",
}


def _age_phrase(minutes: int) -> str:
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{minutes} 分钟前"
    if minutes < 1440:
        return f"{minutes // 60} 小时前"
    return f"{minutes // 1440} 天前"


def _fact_body(content: Mapping, owner_id: str) -> str:
    """世界事实渲染成一句人话。fact 的身份形状由 memory_content 声明。"""
    about = content.get("about") or "某人"
    if about == owner_id:
        about = "我"
    fact = content.get("fact") or ""
    value = content.get("value")
    if fact.startswith("location:"):
        return f"{about} 在 {value}"
    if fact.startswith("channel:"):
        channel = fact.split(":", 2)[2] if fact.count(":") >= 2 else ""
        return (
            f"{about} 在频道 {channel} 里"
            if value == "in"
            else f"{about} 不在频道 {channel} 里了"
        )
    return f"{about}：{value}"


def _body(record: MemoryRecord) -> str:
    """一条记忆在提示词里的正文。只读内容里被显式点名的那几个键。

    摘要本身不带行动者（谁做的在 about / by 里），在这里拼回去。这样同一条
    观察在不同类别下的正文完全一致，渲染时能合并成一行。
    """
    content = record.content
    if record.memory_class is MemoryClass.SEMANTIC:
        return _fact_body(content, record.owner_id)
    summary = content.get("summary") or ""
    if not summary:
        return ""
    actor = content.get("about") or content.get("by")
    mine = (
        bool(content.get("self"))
        or content.get("source") == "self_commitment"
        or actor == record.owner_id
    )
    if mine:
        # 自己的事就说"我"。这里要看行动者是不是记忆的主人，而不是只看内容里
        # 那个 self 标记：短时痕迹不带这个标记，只看标记会让同一条经历渲染成
        # 两行（"我说了…"和"ena说了…"），而它们本来是同一件事。
        return f"我{summary}"
    return f"{actor or '某人'} {summary}"


def recalled_lines(
    result: RecallResult, *, exclude_source_event_ids=()
) -> Tuple[str, ...]:
    """召回结果渲染成提示词里的那几行，顺序就是召回顺序。

    同一段内容只占一行。一条观察会按类别长出好几条记忆（承诺、关系、情节、
    短时痕迹各一条），那是**存储**层面刻意的冗余 —— 它们的衰减和固定行为各不
    相同。但提示词里把同一句话抄四遍毫无价值，所以这里按正文合并，把它们的
    标签并在一起：合并只发生在渲染，存储里那几条一条都没少。

    `exclude_source_event_ids` 只影响提示组合：同一事件仍在近期观察窗口时，
    显示完整观察，不紧接着再显示一份记忆摘要；存储和召回结果本身都不改变。
    """
    if not isinstance(result, RecallResult):
        raise TypeError("recalled_lines() 需要一个 RecallResult")
    excluded = frozenset(exclude_source_event_ids)
    now = result.query.now
    order: List[str] = []
    grouped: Dict[str, Dict] = {}
    for scored in result.memories:
        record = scored.record
        if record.source_event_id in excluded:
            continue
        body = _body(record)
        if not body:
            continue
        tag = _TAGS[record.memory_class]
        entry = grouped.get(body)
        if entry is None:
            minutes = max(0, int((now - record.encoded_at).total_seconds() // 60))
            grouped[body] = {"tags": [tag], "age": _age_phrase(minutes)}
            order.append(body)
        elif tag not in entry["tags"]:
            entry["tags"].append(tag)
    return tuple(
        f"- {grouped[body]['age']}｜{' · '.join(grouped[body]['tags'])}：{body}"
        for body in order
    )


def prompt_block(result: RecallResult, title: str = PROMPT_TITLE) -> str:
    """提示词里那一段。没想起任何东西就返回空字符串 —— 不编一段"我什么都
    不记得"塞进去，空就是空。"""
    lines = recalled_lines(result)
    if not lines:
        return ""
    return "\n".join([title, *lines])


def prompt_projection(result: RecallResult) -> Dict:
    """提示投影的结构化形状，给测试和调试 UI 用。

    它跟 prompt_block() 渲染的是**同一批行**：投影里能看到的东西，就是提示词里
    会出现的东西，不多不少。
    """
    return {
        "owner_id": result.query.owner_id,
        "lines": list(recalled_lines(result)),
        "truncated": result.truncated,
    }


__all__ = [
    "PROMPT_TITLE",
    "prompt_block",
    "prompt_projection",
    "recalled_lines",
]
