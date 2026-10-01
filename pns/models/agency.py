# pns/models/agency.py — Agency 的结果类型与审计日志
#
# Agency 回答一个问题，并且只回答这一个：
#
#     这个角色在这一刻，选择行动吗？如果行动，提出哪一个已声明的动作？
#
# 它不回答：什么时候该考虑（Scheduler）、能不能感知到（Exposure）、说出来
# 像不像本人（Router）、记不记得住（Memory）。这四件事任何一件渗进来都是
# 设计错误。
#
# 这里只放"结果"这个数据类型和它的日志；判断规则在 pns/runtime/agency/。
# 分法跟曝光一样：结果要跟着会话被序列化、被回滚、被调试 UI 读，所以属于
# 领域模型层；规则要读 WorldState 做推导，属于运行时层。
#
# 一条 AgencyRecord 是**审计**，不是世界真相。世界真相只有事件。记录说的是
# "这条到期资格被谁、按哪个策略、判成了什么结果"，包括"什么都没做"。
# "什么都没做"必须留下记录 —— 否则"评估过但决定不动"和"根本没评估"就分不
# 出来，而这两者对下游是完全不同的事实。
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Set, Tuple

from pns.models.action import ActionError, ActionProposal
from pns.models.cognition import (
    QUIET_REASONS,
    REPLY_ACTIVATION_PREFIX,
    CognitionCauseError,
    normalize_causes,
)
from pns.models.frozen import freeze_json_value, thaw_json_value


class AgencyError(ValueError):
    """Agency 记录或日志不合法（结果与字段不自洽、重复身份、存档损坏等）。"""


class AgencyOutcome(str, Enum):
    """一次到期评估的结果码。

    闭集，而且每个码都对应引擎里真实存在的一条分支：

      ACTED                   提案通过校验并已提交，世界历史里有对应事件。
      ABSTAINED               显式不动。**这是合法结果，不是错误**，也不是
                              一句编造出来的台词。
      REJECTED_ILLEGAL        提案在提出的那一刻就不合法（动作不在合法枚举里、
                              前置条件不满足、提案身份撞车、角色对不上）。
      REJECTED_STALE          提出时合法，提交时已经不合法了（时间、地点、
                              频道成员变了）。
      REJECTED_BUDGET         触到了显式声明的安全/算力上限。
      REJECTED_POLICY_ERROR   策略实现自己失败了（模型适配器拿到垃圾、抛异常）。
      REJECTED_UNAVAILABLE    到期时认知不可用（未 Start、暂停、额度用完、停机、
                              故障……）。世界照常发生，这一刻的决定没有发生；
                              原因在 detail 里，见 pns/models/cognition.py。
      QUIET                   前置门判定此刻没什么可想的（睡着了，或者独处、
                              没有新的外部观察、离上次计费认知不到冷却时长），
                              **策略没被问过**。只有引擎能签发（PLAN-1 §9 D2）。

    五个 REJECTED_* 的共同后果完全一样：不产出事件，不产出观察，不留下任何
    半截世界状态。区分它们是为了让"为什么没动"是可查的事实。QUIET 也不产出
    任何世界状态，但它不是"评估过、结论是不行"：根本没评估。
    """

    ACTED = "acted"
    ABSTAINED = "abstained"
    REJECTED_ILLEGAL = "rejected_illegal"
    REJECTED_STALE = "rejected_stale"
    REJECTED_BUDGET = "rejected_budget"
    REJECTED_POLICY_ERROR = "rejected_policy_error"
    REJECTED_UNAVAILABLE = "rejected_unavailable"
    QUIET = "quiet"

    @property
    def acted(self) -> bool:
        return self is AgencyOutcome.ACTED

    @property
    def rejected(self) -> bool:
        return self in _REJECTED_OUTCOMES


_REJECTED_OUTCOMES = frozenset(
    {
        AgencyOutcome.REJECTED_ILLEGAL,
        AgencyOutcome.REJECTED_STALE,
        AgencyOutcome.REJECTED_BUDGET,
        AgencyOutcome.REJECTED_POLICY_ERROR,
        AgencyOutcome.REJECTED_UNAVAILABLE,
    }
)


@dataclass(frozen=True)
class AgencyBudget:
    """显式、确定性的安全/算力上限。

    每一项都有一条真实的执行分支盯着（没有装饰性字段）：超限的后果是一条
    REJECTED_BUDGET 记录，而不是"尽力而为"地截断到能跑为止。

    max_committed_actions_per_session 的计数**从日志推导**，不是另存一个
    计数器：计数器会在存档往返之后归零，于是一份恢复出来的会话可以把上限
    再用一遍。
    """

    # 一条到期资格最多接受几条提案。默认 1 —— 一次"该考虑行动了"对应一个动作。
    max_proposals_per_activation: int = 1
    # 合法动作枚举的条数上限。超了就按确定性顺序截断，并在上下文里标注截断。
    max_legal_actions: int = 32
    # 交给策略的观察条数上限，保留最新的那些。
    max_observations: int = 64
    # 一个会话累计能提交的动作数。
    max_committed_actions_per_session: int = 128
    # 有人当着别人说了话之后，隔多少模拟分钟给在场的其他人一次回话机会。None =
    # 不给（只按固定节拍被考虑）。回不回仍由那个人自己的判断决定；这里只决定
    # "多快被问到"，所以对话的密度由对话本身决定，而不是写死在某个时段上。
    reply_delay_minutes: Optional[int] = None
    # 同一个媒介（频道 / 地点）最近 30 分钟里说过这么多句之后，不再给回话机会，
    # 对话退回固定节拍。它是"一段对话最多多快烧掉单次额度"的上界：没有它，
    # 两个都愿意接话的人会一分钟一句，直到把整次 Start 的额度用完。
    reply_burst_lines: int = 8
    # 前置门的冷却（模拟分钟，PLAN-1 §3）。醒着、独处、自上次计费认知以来没有
    # 新的外部观察时，最多每这么多分钟想一次；睡着时一次都不想。None = 不设
    # 前置门（研究会话与旧调用方的行为）。
    quiet_cooldown_minutes: Optional[int] = None

    # 这里**没有**"允许提交台词动作"的开关，而且不该有：需要台词的动作在本
    # 阶段没有提交路径（见 pns/models/action.py 的 _require_committable），
    # 因为生成 → Router 判分 → 漂移审计那条链还没接进来。安全边界不是预算，
    # 预算是"最多做多少"，边界是"根本不能做"；把边界做成一个调用方能翻的
    # 布尔量，等于没有边界。

    def __post_init__(self) -> None:
        for name in (
            "max_proposals_per_activation",
            "max_legal_actions",
            "max_observations",
            "max_committed_actions_per_session",
            "reply_burst_lines",
        ):
            value = getattr(self, name)
            # bool 是 int 的子类：True 当成"上限 1"会让一个明显写错的配置跑起来。
            if isinstance(value, bool) or not isinstance(value, int):
                raise AgencyError(f"{name} 必须是整数，收到 {value!r}")
            if value <= 0:
                raise AgencyError(f"{name} 必须大于 0，收到 {value}")
        delay = self.reply_delay_minutes
        if delay is not None and (
            isinstance(delay, bool) or not isinstance(delay, int) or not 1 <= delay <= 1440
        ):
            raise AgencyError(f"reply_delay_minutes 必须是 1–1440 的整数或不设，收到 {delay!r}")
        cooldown = self.quiet_cooldown_minutes
        if cooldown is not None and (
            isinstance(cooldown, bool)
            or not isinstance(cooldown, int)
            or not 1 <= cooldown <= 1440
        ):
            raise AgencyError(
                f"quiet_cooldown_minutes 必须是 1–1440 的整数或不设，收到 {cooldown!r}"
            )

    def to_dict(self) -> Dict:
        return {
            "max_proposals_per_activation": self.max_proposals_per_activation,
            "max_legal_actions": self.max_legal_actions,
            "max_observations": self.max_observations,
            "max_committed_actions_per_session": (
                self.max_committed_actions_per_session
            ),
            "reply_delay_minutes": self.reply_delay_minutes,
            "reply_burst_lines": self.reply_burst_lines,
            "quiet_cooldown_minutes": self.quiet_cooldown_minutes,
        }


@dataclass(frozen=True)
class AgencyRecord:
    """一条到期资格被评估之后留下的审计记录。

    身份就是 `due_id`，由调度器那条到期记录推导而来，不是随机生成的：一次
    到期至多被交接一次，所以一条到期至多对应一条记录。这也让"这条到期我处理
    过了"在存档往返之后仍然是同一个判断（跟 ActivationDue.due_id 同样的理由）。

    字段之间的一致性是构造时就锁死的，不靠调用方自觉：

      ACTED    ⇒ 恰好一条提案 + 一个 event_id
      其它     ⇒ 没有提案对象、没有 event_id

    被拒的提案**不**以 ActionProposal 的形式留在记录里，被拒的细节写进
    `detail`。这不是省事：日志对已提交提案的 ID 唯一性是一条硬约束，而"提案
    ID 撞车"本身就是一种拒绝理由 —— 把撞车的那条提案也塞进日志，会让这条
    拒绝记录自己触发同一条约束，整笔事务回滚，到期记录永远卡在待处理。
    """

    due_id: str
    character_id: str
    decided_at: datetime
    outcome: AgencyOutcome
    policy: str = ""
    proposal: Optional[ActionProposal] = None
    event_id: Optional[str] = None
    detail: Mapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        set_ = object.__setattr__

        if not isinstance(self.due_id, str) or not self.due_id:
            raise AgencyError("due_id 必须是非空字符串")
        if not isinstance(self.character_id, str) or not self.character_id:
            raise AgencyError("character_id 必须是非空字符串")
        try:
            set_(self, "outcome", AgencyOutcome(self.outcome))
        except ValueError:
            raise AgencyError(f"未知的 Agency 结果码: {self.outcome!r}") from None

        if not isinstance(self.decided_at, datetime):
            raise AgencyError("decided_at 必须是 datetime（模拟时钟时间）")
        if self.decided_at.tzinfo is not None:
            raise AgencyError(
                f"decided_at 必须是 timezone-naive 的模拟时间，收到 {self.decided_at!r}"
            )
        if not isinstance(self.policy, str):
            raise AgencyError("policy 必须是字符串")

        if self.proposal is not None and not isinstance(self.proposal, ActionProposal):
            raise AgencyError("proposal 必须是 ActionProposal")
        if self.event_id is not None and (
            not isinstance(self.event_id, str) or not self.event_id
        ):
            raise AgencyError("event_id 必须是非空字符串或 None")

        if self.outcome.acted:
            if self.proposal is None:
                raise AgencyError("acted 记录必须带上被接受的提案")
            if self.event_id is None:
                raise AgencyError("acted 记录必须指向它提交出来的那条事件")
            if self.proposal.character_id != self.character_id:
                raise AgencyError(
                    f"提案角色 '{self.proposal.character_id}' 与记录角色 "
                    f"'{self.character_id}' 不一致"
                )
        else:
            if self.proposal is not None:
                raise AgencyError(
                    f"{self.outcome.value} 记录不能带提案对象，细节写进 detail"
                )
            if self.event_id is not None:
                # 没行动却指着一条事件，等于审计说"我没做"而世界说"他做了"。
                raise AgencyError(f"{self.outcome.value} 记录不能指向任何事件")

        if not isinstance(self.detail, Mapping):
            raise AgencyError("detail 必须是字典")
        set_(
            self,
            "detail",
            freeze_json_value(self.detail, path="detail", error=AgencyError),
        )
        if self.outcome is AgencyOutcome.REJECTED_UNAVAILABLE:
            self._require_unavailable_detail()
        if self.outcome is AgencyOutcome.QUIET:
            self._require_quiet_detail()

    def _require_quiet_detail(self) -> None:
        """前置门收尾的记录必须说明理由，而且形状上就证明它没走到策略那一步。

        存档恢复也走这里。它挡的是"记录自己不自洽"；"这条记录真是引擎签发的"
        在运行时由签发表与事务内重算保证，改写整份存档的操作者不在这层的
        防御范围内（PLAN-1 §9 D2）。
        """
        if self.policy:
            raise AgencyError("quiet 记录不能带策略名：没有策略被询问")
        detail = self.detail
        if detail.get("reason") not in QUIET_REASONS:
            raise AgencyError(
                f"quiet 记录的 reason 必须是 {sorted(QUIET_REASONS)} 之一，"
                f"收到 {detail.get('reason')!r}"
            )
        if "audit" in detail or "rationale" in detail:
            raise AgencyError("quiet 记录不能带判分凭据或策略说法")
        if self.due_id.startswith(REPLY_ACTIVATION_PREFIX):
            # 回话机会有自己的免单路径（reply_lapsed），不走前置门。
            raise AgencyError("回话机会不能被前置门收尾")

    def _require_unavailable_detail(self) -> None:
        """认知不可用的记录必须说清楚为什么，而且不能声称问过策略。

        存档恢复也走这里，所以一条被改过原因、或者被塞进策略名的不可用记录
        在加载时就会被拒绝。
        """
        if self.policy:
            # 不可用意味着这一刻根本没有做决定：写上策略名，等于声称策略被
            # 问过、给出了"不动"的答案。
            raise AgencyError("rejected_unavailable 记录不能带策略名：没有策略被询问")
        detail = self.detail
        if detail.get("reason") != "cognition_unavailable":
            raise AgencyError("rejected_unavailable 记录的 reason 必须是 cognition_unavailable")
        causes = detail.get("causes")
        try:
            normalized = normalize_causes(causes if causes is not None else ())
        except CognitionCauseError as e:
            raise AgencyError(f"rejected_unavailable 记录的原因不合法：{e}") from None
        if list(causes) != [cause.value for cause in normalized]:
            raise AgencyError("rejected_unavailable 记录的原因必须去重并排序")
        interval = detail.get("interval")
        if isinstance(interval, bool) or not isinstance(interval, int) or interval < 0:
            raise AgencyError("rejected_unavailable 记录必须指向一个认知时间线区间序号")

    def __hash__(self) -> int:
        return hash(self.due_id)

    @property
    def proposal_id(self) -> Optional[str]:
        return self.proposal.proposal_id if self.proposal is not None else None

    def to_dict(self) -> Dict:
        return {
            "due_id": self.due_id,
            "character_id": self.character_id,
            "decided_at": self.decided_at.isoformat(),
            "outcome": self.outcome.value,
            "policy": self.policy,
            "proposal": self.proposal.to_dict() if self.proposal is not None else None,
            "event_id": self.event_id,
            "detail": thaw_json_value(self.detail),
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "AgencyRecord":
        if not isinstance(payload, Mapping):
            raise AgencyError("Agency 记录必须是字典")
        for required in ("due_id", "character_id", "decided_at", "outcome"):
            if required not in payload:
                raise AgencyError(f"Agency 记录缺少必填字段: {required}")
        decided_at = payload["decided_at"]
        if isinstance(decided_at, str):
            try:
                decided_at = datetime.fromisoformat(decided_at)
            except ValueError:
                raise AgencyError(
                    f"无法解析的 decided_at: {payload['decided_at']!r}"
                ) from None
        raw_proposal = payload.get("proposal")
        proposal = None
        if raw_proposal is not None:
            try:
                proposal = ActionProposal.from_dict(raw_proposal)
            except ActionError as e:
                raise AgencyError(f"Agency 记录里的提案不合法: {e}") from e
        return cls(
            due_id=payload["due_id"],
            character_id=payload["character_id"],
            decided_at=decided_at,
            outcome=payload["outcome"],
            policy=payload.get("policy", ""),
            proposal=proposal,
            event_id=payload.get("event_id"),
            detail=payload.get("detail", {}),
        )


class AgencyLog:
    """一个会话里唯一一份 Agency 审计日志。

    只追加。对外只提供读取；带下划线的写入/回滚方法只供 Agency 的提交事务
    使用 —— 绕开事务写进来的记录回滚不掉，会留下"审计说动了、世界说没动"。

    两条唯一性硬约束：

      1. 一条 due_id 至多一条记录 —— 交接只发生一次。
      2. 已提交提案的 proposal_id 全局唯一 —— 同一条提案不能被提交两次。
    """

    def __init__(self, records: Iterable[AgencyRecord] = ()):
        self._records: List[AgencyRecord] = []
        self._by_due: Dict[str, int] = {}
        self._proposal_ids: Set[str] = set()
        for record in records:
            self._append(record)

    # ── 写入（只给 Agency 提交事务用） ──────────────────────────────────
    def _check_can_append(self, record: AgencyRecord) -> None:
        """落库前的纯校验：只看能不能落，不改任何状态。"""
        if not isinstance(record, AgencyRecord):
            raise AgencyError("只能向 Agency 日志追加 AgencyRecord")
        if record.due_id in self._by_due:
            raise AgencyError(f"到期 '{record.due_id}' 已经被评估过")
        proposal_id = record.proposal_id
        if proposal_id is not None and proposal_id in self._proposal_ids:
            raise AgencyError(f"重复的 proposal_id: {proposal_id}")
        if self._records and record.decided_at < self._records[-1].decided_at:
            # 决定时刻只会随模拟时钟前进。倒流意味着有人在拼接两个不同时刻的状态。
            raise AgencyError(
                f"记录 '{record.due_id}' 的决定时间 "
                f"{record.decided_at.isoformat()} 早于上一条 "
                f"{self._records[-1].decided_at.isoformat()}"
            )

    def _append(self, record: AgencyRecord) -> int:
        self._check_can_append(record)
        self._records.append(record)
        self._by_due[record.due_id] = len(self._records) - 1
        if record.proposal_id is not None:
            self._proposal_ids.add(record.proposal_id)
        return len(self._records) - 1

    def _rollback_to(self, length: int) -> None:
        if not isinstance(length, int) or isinstance(length, bool):
            raise AgencyError("回滚长度必须是整数")
        if length < 0 or length > len(self._records):
            raise AgencyError(f"回滚长度越界: {length}")
        del self._records[length:]
        self._by_due = {
            record.due_id: index for index, record in enumerate(self._records)
        }
        self._proposal_ids = {
            record.proposal_id
            for record in self._records
            if record.proposal_id is not None
        }

    # ── 读取 ────────────────────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[AgencyRecord]:
        return iter(tuple(self._records))

    def records(self) -> Tuple[AgencyRecord, ...]:
        return tuple(self._records)

    def has(self, due_id: str) -> bool:
        return due_id in self._by_due

    def get(self, due_id: str) -> AgencyRecord:
        index = self._by_due.get(due_id)
        if index is None:
            raise AgencyError(f"未知的 due_id: {due_id}")
        return self._records[index]

    def for_character(self, character_id: str) -> Tuple[AgencyRecord, ...]:
        return tuple(r for r in self._records if r.character_id == character_id)

    def for_outcome(self, outcome) -> Tuple[AgencyRecord, ...]:
        outcome = AgencyOutcome(outcome)
        return tuple(r for r in self._records if r.outcome is outcome)

    def committed_actions(self) -> int:
        """本会话累计提交的动作数。会话级预算就是按它判的。

        从记录推导而不是另存计数器：存档往返之后它必须还是同一个数，否则
        恢复出来的会话可以把上限再用一遍。
        """
        return sum(1 for record in self._records if record.outcome.acted)

    def proposal_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._proposal_ids))

    def latest(self) -> Optional[AgencyRecord]:
        return self._records[-1] if self._records else None

    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> Dict:
        return {
            "records": [
                {"sequence": index, **record.to_dict()}
                for index, record in enumerate(self._records)
            ]
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "AgencyLog":
        """从持久化形状恢复；序号不连续、记录损坏、身份撞车一律拒绝。"""
        if not isinstance(payload, Mapping):
            raise AgencyError("Agency 日志必须是字典")
        entries = payload.get("records", [])
        if not isinstance(entries, list):
            raise AgencyError("Agency 日志的 records 必须是数组")
        log = cls()
        for expected, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                raise AgencyError(f"Agency 日志第 {expected} 项必须是字典")
            sequence = entry.get("sequence")
            if (
                not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence != expected
            ):
                raise AgencyError(
                    f"Agency 日志 sequence 不连续：第 {expected} 项收到 {sequence!r}"
                )
            log._append(AgencyRecord.from_dict(entry))
        return log
