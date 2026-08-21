# pns/runtime/autonomy/generation.py — 角色生成层的适配与解析
#
# 这一层回答的问题只有一个：**这个角色，为这个已经选定的动作，说哪一句？**
#
# 它不回答：什么时候该考虑（Scheduler）、要不要动、动哪个（Agency）、说得
# 像不像本人（Router）、记不记得住（Memory）。
#
# 三条硬约束：
#
#   1. **模型输出是不可信输入。** 生成器交回来的东西先过 parse_line()，
#      变成一个类型化的结果，再由 Agency 的校验通道重新判身份、动作、目标、
#      前置条件与预算。未声明的键一律拒绝，不是丢掉 —— 悄悄丢掉会让调用方
#      以为它生效了。
#   2. **模型不选自己是谁。** 输出里只有一句话。角色 ID、动作、目标全部来自
#      Agency 的上下文；一个能自报 character_id 的输出，等于让模型决定它在
#      扮演谁。
#   3. **生成不是提交。** 这里产出的只是一条 ActionProposal，而需要台词的
#      提案没有判分凭据就提交不了（见 pns/models/action.py 的
#      _require_committable）。所以生成失败、输出是垃圾、模型胡说八道，
#      后果都只是一条被拒的审计记录，不是一次半提交的世界变更。
#
# 确定性适配器（ScriptedLineGenerator）跟真实模型适配器走的是同一条通道 ——
# 这是"完整回路可以不联网跑完"的全部实现方式，不需要在运行时里埋测试分支。
from typing import Callable, Mapping, Optional, Tuple

from pns.models.action import ActionError, ActionProposal, LegalAction
from pns.runtime.agency.context import AgencyContext
from pns.runtime.agency.policy import (
    AgencyPolicy,
    AgencyPolicyError,
    PolicyDecision,
    derived_proposal_id,
)
from pns.runtime.autonomy.context import (
    GenerationContext,
    build_generation_context,
)
from pns.runtime.memory.projection import recalled_lines

# 一句台词的长度上限。它不是审美判断，是安全预算：一个失控的生成会把整份
# 上下文吐回来，而那份上下文里有这个角色自己的全部观察与回忆 —— 原样进世界
# 历史、再被别人观察到，就是一次绕过曝光的泄漏。
MAX_LINE_CHARS = 2000

# 生成器交回来的字典里，允许出现的键。**只有一个**：这句话。
_ALLOWED_KEYS = frozenset({"text"})


class GenerationError(ValueError):
    """这次生成没能交出一句可用的台词。

    `retryable` 区分两类失败，而且这个区分有真实后果：模型暂时不可用
    （retryable=True）值得按重试预算再来一次；提示词模板坏了、输出结构不对
    （retryable=False）再试一百次也一样，直接烧掉这次到期资格。
    默认 False —— 说不清楚的失败按"别再试了"处理，免得一条到期资格被一个
    永远失败的生成器卡死在待处理。
    """

    def __init__(self, *args, retryable: bool = False):
        super().__init__(*args)
        self.retryable = bool(retryable)


def parse_line(raw, context: GenerationContext) -> str:
    """把生成器交回来的**原始形状**变成一句台词。

    接受两种形状，别的一律拒绝：

        "……在的哦"            纯字符串
        {"text": "……在的哦"}   只有 text 这一个键的字典

    刻意不接受 `{"text": ..., "character_id": ...}` 之类：那是模型在试图
    决定自己是谁、或者往事件 payload 里塞未声明的键。两者都必须响亮失败。
    """
    if isinstance(raw, Mapping):
        extra = sorted(set(raw) - _ALLOWED_KEYS)
        if extra:
            raise GenerationError(
                f"生成输出里有未声明的键: {', '.join(extra)}"
            )
        raw = raw.get("text")
    if isinstance(raw, str):
        text = raw.strip()
    else:
        raise GenerationError(
            f"生成输出必须是字符串或 {{'text': ...}}，收到 {type(raw).__name__}"
        )
    if not text:
        raise GenerationError("生成输出是空的 —— 没说出口的话不是一句台词")
    if len(text) > MAX_LINE_CHARS:
        raise GenerationError(
            f"生成输出超过 {MAX_LINE_CHARS} 字（收到 {len(text)}），"
            "不接受截断：截断会把一句被砍掉一半的话当成角色说完了"
        )
    return text


class LineGenerator:
    """生成器接口。

    `generate()` 拿到的是一个不可变的、角色作用域的上下文，除此之外什么都
    没有 —— 它没有会话、没有世界、没有事件历史，所以它没有任何提交的能力，
    只有建议的能力。
    """

    name = "generator"

    def generate(self, context: GenerationContext) -> object:
        raise NotImplementedError


class ScriptedLineGenerator(LineGenerator):
    """按剧本走：角色 ID → 一句话（或者一个拿上下文算话的函数）。

    存在的意义是让**完整回路可以不联网跑完**：确定性适配器和真实模型适配器
    走的是同一条解析与校验通道，所以用它跑出来的结论对真实路径也成立。
    剧本里查不到这个角色就抛一个不可重试的失败 —— 剧本没写的情况不该变成
    "随便编一句"。
    """

    name = "scripted"

    def __init__(self, lines: Mapping[str, object], *, name: Optional[str] = None):
        if not isinstance(lines, Mapping):
            raise GenerationError("剧本必须是字典")
        self._lines = dict(lines)
        if name is not None:
            self.name = name

    def generate(self, context: GenerationContext) -> object:
        if context.character_id not in self._lines:
            raise GenerationError(
                f"剧本里没有角色 '{context.character_id}' 的台词", retryable=False
            )
        entry = self._lines[context.character_id]
        return entry(context) if callable(entry) else entry


def first_authored_action(context: AgencyContext) -> Optional[LegalAction]:
    """默认的动作选择：按确定性顺序挑第一个需要台词的合法动作。

    需要台词的动作恰恰是 P9 里挑不了的那些 —— 生成层接上来之后，它们才第一次
    真的可选。挑不到就返回 None（弃权），不退而求其次去做一个别的动作：
    "没什么可说的"和"那就换个动作吧"是两个不同的判断，后者属于策略。
    """
    for legal in context.legal_actions:
        if legal.requires_authored_text:
            return legal
    return None


class AuthoredLinePolicy(AgencyPolicy):
    """把"选一个需要台词的动作 + 为它生成一句话"接成一个 Agency 策略。

    它仍然只是**建议者**：合法性、前置条件、预算、判分凭据、提交事务全部在
    引擎那一侧。它跟别的策略走的是同一条校验通道，没有捷径。

    召回是可选的：给了召回服务，这个角色**自己的**记忆就会渲染成几行进生成
    上下文。收窄发生在 `recall_for(context.character_id)` 这一行 —— 一行，
    显式，可以一眼审查完（跟 build_agency_context 同一条规矩）。

    显示名来自构造时传进来的 `names`，**不**来自模型输出：一个能自报显示名的
    输出等于让模型决定它在扮演谁，而那个名字会原样进事件 payload、再被别人
    观察到。
    """

    name = "authored_line"

    def __init__(
        self,
        generator: LineGenerator,
        *,
        recall=None,
        chooser: Optional[Callable[[AgencyContext], Optional[LegalAction]]] = None,
        names: Optional[Mapping[str, str]] = None,
        name: Optional[str] = None,
    ):
        if not callable(getattr(generator, "generate", None)):
            raise AgencyPolicyError("生成器必须提供 generate()")
        if recall is not None and not callable(getattr(recall, "recall_for", None)):
            raise AgencyPolicyError("召回服务必须提供 recall_for()")
        if chooser is not None and not callable(chooser):
            raise AgencyPolicyError("chooser 必须是可调用对象")
        if names is not None and not isinstance(names, Mapping):
            raise AgencyPolicyError("names 必须是字典")
        self._generator = generator
        self._recall = recall
        self._chooser = chooser if chooser is not None else first_authored_action
        self._names = dict(names or {})
        if name is not None:
            if not isinstance(name, str) or not name:
                raise AgencyPolicyError("name 必须是非空字符串")
            self.name = name

    @property
    def generator(self) -> LineGenerator:
        return self._generator

    def decide(self, context: AgencyContext) -> PolicyDecision:
        choice = self._chooser(context)
        if choice is None:
            # 显式不动。合法结果，不是错误，也不是一句编出来的台词。
            return PolicyDecision(rationale="no action chosen for this activation")
        if not isinstance(choice, LegalAction):
            raise AgencyPolicyError(
                f"chooser 必须返回 LegalAction 或 None，收到 {type(choice).__name__}"
            )
        if not context.has_legal(choice.action_id, choice.target_id):
            # 挑了一个不在合法枚举里的组合。引擎那边也会拦，但在这里就失败
            # 能省掉一次白跑的生成 —— 而且理由说得清楚得多。
            raise AgencyPolicyError(
                f"chooser 选了一个此刻不合法的动作: "
                f"{choice.action_id.value} → {choice.target_id!r}"
            )

        proposal_id = derived_proposal_id(context)
        if not choice.requires_authored_text:
            # 不需要台词的动作根本不进生成层：没有话要说的时候造一句，
            # 就是把"不知道说什么"伪装成"说了点什么"。
            return self._proposal(context, choice, proposal_id, payload={})

        generation_context = self._generation_context(context, choice)
        try:
            raw = self._generator.generate(generation_context)
        except GenerationError as e:
            raise AgencyPolicyError(f"生成失败: {e}", retryable=e.retryable) from e
        except Exception as e:
            # 生成器背后是网络和模型，什么都可能发生。说不清楚的失败按可重试
            # 处理 —— 这一档由上层的重试预算兜底，不会无限试下去。
            raise AgencyPolicyError(
                f"生成器调用失败: {type(e).__name__}: {e}", retryable=True
            ) from e

        try:
            text = parse_line(raw, generation_context)
        except GenerationError as e:
            # 输出结构不对是**不可重试**的：同一个坏模板会一直吐同样的垃圾。
            raise AgencyPolicyError(f"生成输出不合法: {e}", retryable=False) from e

        payload = {"text": text}
        char_name = self._names.get(context.character_id)
        if char_name:
            payload["char_name"] = char_name
        return self._proposal(context, choice, proposal_id, payload=payload)

    def _proposal(
        self, context: AgencyContext, choice: LegalAction, proposal_id: str, *, payload
    ) -> PolicyDecision:
        try:
            proposal = ActionProposal(
                proposal_id=proposal_id,
                character_id=context.character_id,
                action_id=choice.action_id,
                target_id=choice.target_id,
                payload=payload,
            )
        except ActionError as e:
            raise AgencyPolicyError(f"提案形状不合法: {e}") from e
        return PolicyDecision(proposals=(proposal,), rationale="authored line")

    def _generation_context(
        self, context: AgencyContext, choice: LegalAction
    ) -> GenerationContext:
        recalled: Tuple[str, ...] = ()
        truncated = False
        if self._recall is not None:
            # 收窄就是这一行：只取这个角色自己的记忆。投影层再按白名单删减
            # （没有记忆 ID、没有曝光理由码、没有显著度、没有 provenance）。
            result = self._recall.recall_for(context.character_id)
            recalled = recalled_lines(result)
            truncated = result.truncated
        return build_generation_context(
            context, choice, recalled, recall_truncated=truncated
        )


__all__ = [
    "MAX_LINE_CHARS",
    "AuthoredLinePolicy",
    "GenerationError",
    "LineGenerator",
    "ScriptedLineGenerator",
    "first_authored_action",
    "parse_line",
]
