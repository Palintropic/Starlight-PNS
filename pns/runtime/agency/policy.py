# pns/runtime/agency/policy.py — "要不要动、动哪个"的选择实现
#
# 策略是**建议者**，不是权威。它交回来的东西只有一种：一个 PolicyDecision，
# 里面装着零条或多条提案。它改不了世界，也决定不了自己的提案会不会被接受 ——
# 合法性、前置条件、预算、提交事务全部在引擎那一侧，任何策略走的都是同一条
# 校验通道。
#
# 这条分工是模型驱动选择能被允许存在的唯一前提：
#
#     确定性策略（测试、研究、回归）   ─┐
#                                      ├─→ 同一个 PolicyDecision 类型
#     模型驱动适配器（以后的真实运行） ─┘        ↓
#                                       引擎校验 → 可能被接受
#
# 模型永远只是这条链的**输入端**。它答错了、答成垃圾、干脆抛异常，结果都只是
# 一条被拒的审计记录，不是一次半提交的世界变更。
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence, Tuple

from pns.models.action import ActionError, ActionProposal
from pns.runtime.agency.context import AgencyContext


class AgencyPolicyError(ValueError):
    """策略实现自己失败了（返回形状不对、动作名解析不了、内部抛异常）。

    引擎捕获它并记成一条 REJECTED_POLICY_ERROR，不让它冒泡穿过提交事务：
    一个策略实现出问题，不应该变成整个运行时的异常。

    `retryable` 是策略对这次失败的**自述**：模型暂时不可用跟提示词模板写错
    是两件事，前者再试一次就好，后者试一百次也一样。它只是一条被记进审计
    细节的声明，不是权限 —— 重试要不要真的发生、发生几次，由上层的重试预算
    决定（见 pns/runtime/autonomy/outcome.py）。默认是 False：说不清楚的
    失败按"别再试了"处理，免得一个永远失败的策略把一条到期资格卡死在待处理。
    """

    def __init__(self, *args, retryable: bool = False):
        super().__init__(*args)
        self.retryable = bool(retryable)


@dataclass(frozen=True)
class PolicyDecision:
    """一次判断的产物。

    `proposals` 为空就是**显式不动**。这是合法结果，不是错误码，也不是一句
    编出来的台词 —— "这个角色决定什么都不做"和"这个角色没被问过"是两件不同
    的事，前者会留下一条 ABSTAINED 审计记录。
    """

    proposals: Tuple[ActionProposal, ...] = ()
    rationale: str = ""
    detail: Mapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        for proposal in self.proposals:
            if not isinstance(proposal, ActionProposal):
                raise AgencyPolicyError("PolicyDecision 只能装 ActionProposal")
        if not isinstance(self.rationale, str):
            raise AgencyPolicyError("rationale 必须是字符串")
        if not isinstance(self.detail, Mapping):
            raise AgencyPolicyError("detail 必须是字典")

    @property
    def abstains(self) -> bool:
        return not self.proposals


class AgencyPolicy:
    """策略接口。

    `decide()` 必须是**只读**的：它拿到的是一个不可变上下文，除此之外不该
    碰任何东西。会话、世界、事件历史都不在它手上 —— 这不是靠约定，是因为
    这些东西根本没传给它。
    """

    name = "policy"

    def decide(self, context: AgencyContext) -> PolicyDecision:
        raise NotImplementedError


def derived_proposal_id(context: AgencyContext, index: int = 0) -> str:
    """从到期身份推导出的提案 ID。

    确定性策略一律用它，不用随机 ID：同一份存档恢复出来、同一个上下文重跑，
    提案身份必须还是同一个，否则"这条提案提交过没有"就没法幂等地判断
    （跟 ActivationDue.due_id 同样的理由）。
    """
    return f"{context.activation.due_id}#{index}"


class AbstainPolicy(AgencyPolicy):
    """永远不动。

    默认策略就是它，这是刻意的：一个还没接生成层的运行时，最诚实的行为是
    什么都不做，而不是随便挑个动作证明自己活着。
    """

    name = "abstain"

    def decide(self, context: AgencyContext) -> PolicyDecision:
        return PolicyDecision(rationale="default policy takes no action")


class FirstLegalActionPolicy(AgencyPolicy):
    """按确定性顺序挑第一个不需要台词的合法动作。

    存在的意义是给测试和回归一个**真的会动**、但完全可复现的策略。需要台词
    的动作一律跳过：台词属于角色生成层，这里造一句就是在伪造角色行为。
    没有可挑的就弃权。
    """

    name = "first_legal"

    def decide(self, context: AgencyContext) -> PolicyDecision:
        options = context.legal_without_authored_text()
        if not options:
            return PolicyDecision(rationale="no action available without authored text")
        chosen = options[0]
        return PolicyDecision(
            proposals=(
                ActionProposal(
                    proposal_id=derived_proposal_id(context),
                    character_id=context.character_id,
                    action_id=chosen.action_id,
                    target_id=chosen.target_id,
                ),
            ),
            rationale="first legal action in deterministic order",
        )


class ScriptedPolicy(AgencyPolicy):
    """按剧本走：due_id 或角色 ID → 决定。

    给测试用的确定性策略。查不到剧本就弃权 —— 剧本没写的情况不该变成"随便
    动一下"。
    """

    name = "scripted"

    def __init__(self, script: Mapping[str, PolicyDecision]):
        if not isinstance(script, Mapping):
            raise AgencyPolicyError("剧本必须是字典")
        for value in script.values():
            if not isinstance(value, PolicyDecision):
                raise AgencyPolicyError("剧本的值必须是 PolicyDecision")
        self._script = dict(script)

    def decide(self, context: AgencyContext) -> PolicyDecision:
        for key in (context.activation.due_id, context.character_id):
            if key in self._script:
                return self._script[key]
        return PolicyDecision(rationale="no scripted decision for this activation")


class ModelBackedPolicy(AgencyPolicy):
    """模型驱动选择的适配器。

    它只做一件事：把一个外部选择器交回来的**原始形状**翻译成 PolicyDecision。
    它刻意**不**判断合法性 —— 那是引擎的事，而且必须是引擎的事：如果适配器
    自己筛一遍再交上去，就等于有两处在决定什么能被接受，而其中一处的输入
    来自模型。

    构造只吃一个可调用对象：它拿不到 SessionState、拿不到世界、拿不到事件
    历史，所以它没有任何提交的能力，只有建议的能力。

    选择器可以返回：
        None / {} / {"action_id": None}   → 弃权
        {"action_id": ..., "target_id": ..., "payload": {...}}  → 一条提案
        [{...}, {...}]                     → 多条提案（会撞上每次到期的提案上限）
    别的一律 AgencyPolicyError，由引擎记成 REJECTED_POLICY_ERROR。
    """

    name = "model_backed"

    def __init__(
        self,
        select: Callable[[AgencyContext], object],
        *,
        name: Optional[str] = None,
    ):
        if not callable(select):
            raise AgencyPolicyError("模型适配器需要一个可调用的选择器")
        self._select = select
        if name is not None:
            if not isinstance(name, str) or not name:
                raise AgencyPolicyError("name 必须是非空字符串")
            self.name = name

    def decide(self, context: AgencyContext) -> PolicyDecision:
        try:
            raw = self._select(context)
        except AgencyPolicyError:
            raise
        except Exception as e:  # 选择器背后是网络和模型，什么都可能发生
            raise AgencyPolicyError(
                f"选择器调用失败: {type(e).__name__}: {e}"
            ) from e

        if raw is None:
            return PolicyDecision(rationale="model selected no action")
        entries: Sequence
        if isinstance(raw, Mapping):
            entries = (raw,)
        elif isinstance(raw, (list, tuple)):
            entries = tuple(raw)
        else:
            raise AgencyPolicyError(
                f"选择器必须返回字典、字典数组或 None，收到 {type(raw).__name__}"
            )

        proposals = []
        for index, entry in enumerate(entries):
            proposal = self._parse(context, entry, index)
            if proposal is not None:
                proposals.append(proposal)
        return PolicyDecision(
            proposals=tuple(proposals),
            rationale="model selection",
        )

    def _parse(
        self, context: AgencyContext, entry: object, index: int
    ) -> Optional[ActionProposal]:
        if not isinstance(entry, Mapping):
            raise AgencyPolicyError(
                f"选择器返回的第 {index} 项必须是字典，收到 {type(entry).__name__}"
            )
        action_id = entry.get("action_id")
        if action_id is None:
            return None
        payload = entry.get("payload", {})
        if not isinstance(payload, Mapping):
            raise AgencyPolicyError(f"选择器返回的第 {index} 项 payload 必须是字典")
        proposal_id = entry.get("proposal_id") or derived_proposal_id(context, index)
        try:
            return ActionProposal(
                proposal_id=proposal_id,
                character_id=context.character_id,
                action_id=action_id,
                target_id=entry.get("target_id"),
                payload=payload,
            )
        except ActionError as e:
            # 形状不对（未知动作、目标不符、payload 键没声明）在这里就变成
            # 策略失败：它连一条格式合法的提案都没能给出来。
            raise AgencyPolicyError(f"选择器返回的第 {index} 项不是合法提案: {e}") from e


def default_policy() -> AgencyPolicy:
    """引擎在没被指定策略时用的那一个 —— 永远是"不动"。

    每次新建一个，不做模块级缓存：策略是无状态的，但一个模块级实例会让
    "这个包里没有任何跨会话共享的活对象"这条检查变得需要解释。
    """
    return AbstainPolicy()


__all__ = [
    "AbstainPolicy",
    "AgencyPolicy",
    "AgencyPolicyError",
    "FirstLegalActionPolicy",
    "ModelBackedPolicy",
    "PolicyDecision",
    "ScriptedPolicy",
    "default_policy",
    "derived_proposal_id",
]
