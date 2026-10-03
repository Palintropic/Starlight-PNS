# pns/interfaces/content_maintenance.py — 冷维护：对内容冲突记下项目所有者的决定
#
# CONTENT-4 过渡设计 v2：服务器遇到作息门不齐的世界，会把冲突存盘、关闭、释放
# 所有权，然后拒绝恢复。决定由这里来记：在同一个容器里另起一个进程，独占打开
# 世界（不运行、不碰时钟），逐条记决定，关闭。和服务器的互斥只靠现有的所有权锁。
#
# 这里只走生命周期服务的公开路径和协调器的 decide_content，不直接提交任何会话
# 事件，也不起时钟 worker、不碰认知。
#
# "完成"只有一个判据：关闭之后从磁盘重新读出账本，要求的每一条决定都在，关闭是
# clean 的、耐久的、目录同步有证据的；而且这次运行里没有任何一步报过错。内存里
# 看起来 adopted、某个调用没报错，都不算。
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from pns.models.content_ledger import ConflictStatus, ContentLedger
from pns.runtime.persistence import CheckpointPolicy

from .composition import WorldControlPlane

DECISIONS = ("adopted", "declined", "deferred")

# 维护会话固定的保存策略：没有自动 checkpoint（每条决定之后显式存），关闭时
# 一定存。不沿用服务器的策略，免得某个配置把 on_close 关掉。
MAINTENANCE_POLICY = CheckpointPolicy(every_boundaries=None, on_close=True)


class MaintenanceError(RuntimeError):
    """这次维护在动手之前就做不了（参数不对、不是正式世界）。"""


@dataclass(frozen=True)
class ConflictView:
    """磁盘上的一条冲突记录，以及它此刻还能不能被决定。"""

    conflict_id: str
    subject: str
    status: str
    # 它针对的是不是此刻的已采用版本。不是的话，采用会被账本拒绝。
    current_base: bool

    def to_dict(self) -> Dict:
        return {
            "conflict_id": self.conflict_id,
            "subject": self.subject,
            "status": self.status,
            "current_base": self.current_base,
        }


@dataclass
class DecisionOutcome:
    conflict_id: str
    requested: str
    # recorded / already / not_attempted / failed
    result: str = "not_attempted"
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "conflict_id": self.conflict_id,
            "requested": self.requested,
            "result": self.result,
            "error": self.error,
        }


@dataclass
class MaintenanceReport:
    world_id: str
    outcomes: List[DecisionOutcome] = field(default_factory=list)
    # 这次运行里报过的每一个错（按发生顺序）。非空就不算完成。
    errors: List[str] = field(default_factory=list)
    close: Optional[Dict] = None
    # 关闭之后从磁盘重新读出来的冲突记录。读不出来是 None。
    disk: Optional[Tuple[ConflictView, ...]] = None
    # 打开时作息门挡住的主体（按当前内容包）。门本来就齐是空列表。
    blocked_at_open: List[Dict] = field(default_factory=list)

    @property
    def still_blocked(self) -> List[Dict]:
        """打开时挡门、而这次没有被采用（含"磁盘上本来就已采用"）的主体。

        非空说明即使这次完成了，下一次恢复仍然会被挡住。它不参与退出码：退出码
        回答的是"要求的决定落没落盘"，这一条回答"门会不会齐"。
        """
        adopted = {
            outcome.conflict_id
            for outcome in self.outcomes
            if outcome.requested == "adopted" and outcome.result in ("recorded", "already")
        }
        return [s for s in self.blocked_at_open if s.get("conflict_id") not in adopted]

    def disk_status(self, conflict_id: str) -> Optional[str]:
        for view in self.disk or ():
            if view.conflict_id == conflict_id:
                return view.status
        return None

    @property
    def on_disk(self) -> bool:
        """要求的每一条决定都在重新读出的磁盘账本里。"""
        if self.disk is None:
            return False
        return all(
            self.disk_status(outcome.conflict_id) == outcome.requested
            for outcome in self.outcomes
        )

    @property
    def strongly_durable(self) -> bool:
        """关闭是 clean 的、耐久的，目录同步（含历史分卷）有证据。"""
        close = self.close or {}
        return (
            close.get("clean") is True
            and close.get("durable") is True
            and close.get("directory_synced") is True
        )

    @property
    def complete(self) -> bool:
        return not self.errors and self.on_disk and self.strongly_durable

    @property
    def exit_code(self) -> int:
        return 0 if self.complete else 1

    def to_dict(self) -> Dict:
        return {
            "world_id": self.world_id,
            "complete": self.complete,
            "on_disk": self.on_disk,
            "strongly_durable": self.strongly_durable,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "errors": list(self.errors),
            "close": None
            if self.close is None
            else {
                key: self.close.get(key)
                for key in ("closed", "clean", "durable", "directory_synced", "revision")
            },
            "disk": None if self.disk is None else [view.to_dict() for view in self.disk],
            "still_blocked": self.still_blocked,
        }


def _views(ledger: Optional[ContentLedger]) -> Tuple[ConflictView, ...]:
    if ledger is None:
        return ()
    return tuple(
        ConflictView(
            conflict_id=conflict.conflict_id,
            subject=conflict.subject,
            status=conflict.status.value,
            current_base=conflict.adopted_fingerprint
            == ledger.adopted_fingerprint(conflict.subject),
        )
        for conflict in ledger.conflicts
    )


def read_conflicts(plane: WorldControlPlane, world_id: str) -> Tuple[ConflictView, ...]:
    """只读：磁盘上这个世界的冲突记录。不拿所有权、不写任何东西。"""
    archive = plane.store.load(world_id, history=False)
    payload = archive.state.get("content")
    if payload is None:
        raise MaintenanceError(f"世界 '{world_id}' 没有内容账本（不是正式世界）")
    return _views(ContentLedger.from_dict(payload))


def _parse(decisions: Sequence[Tuple[str, str]]) -> List[DecisionOutcome]:
    outcomes: List[DecisionOutcome] = []
    seen = set()
    for conflict_id, decision in decisions:
        if decision not in DECISIONS:
            raise MaintenanceError(f"未知的决定: {decision!r}（只能是 {', '.join(DECISIONS)}）")
        if not isinstance(conflict_id, str) or not conflict_id:
            raise MaintenanceError("conflict_id 不能为空")
        if conflict_id in seen:
            raise MaintenanceError(f"同一条冲突给了两次决定: {conflict_id}")
        seen.add(conflict_id)
        outcomes.append(DecisionOutcome(conflict_id=conflict_id, requested=decision))
    if not outcomes:
        raise MaintenanceError("没有要记的决定")
    return outcomes


def run_content_decisions(
    plane: WorldControlPlane, world_id: str, decisions: Sequence[Tuple[str, str]]
) -> MaintenanceReport:
    """独占打开世界，逐条记决定，关闭，再从磁盘读回来核对。

    世界正被别的进程（例如服务器）持有时，打开就抛 `WorldAlreadyOwned`，什么都
    没改；调用方应该提示先关闭世界。其余失败都记进报告，不抛出。
    """
    outcomes = _parse(decisions)
    report = MaintenanceReport(world_id=world_id, outcomes=outcomes)
    adapters = plane.build_adapters(plane.registry())
    world = plane.service.restore(
        world_id,
        adapters=adapters,
        checkpoint_policy=MAINTENANCE_POLICY,
        clock=None,
        start=False,
    )
    try:
        held = world.held
        report.blocked_at_open = list(held["subjects"]) if held is not None else []
        ledger = world.state.content
        if ledger is None:
            report.errors.append(f"世界 '{world_id}' 没有内容账本（不是正式世界）")
        else:
            _decide_all(world, report)
    except Exception as e:
        # 任何意料之外的失败都进报告，然后照常关闭、读回磁盘：调用方要的是
        # "最终磁盘是什么样"和"这次哪一步失败过"，两样都不能因为一个异常丢掉。
        report.errors.append(f"维护中途失败: {type(e).__name__}: {e}")
    finally:
        try:
            report.close = world.close("content_maintenance")
        except Exception as e:
            report.errors.append(f"关闭失败: {type(e).__name__}: {e}")
    try:
        report.disk = read_conflicts(plane, world_id)
    except Exception as e:
        report.errors.append(f"关闭之后从磁盘读回失败: {type(e).__name__}: {e}")
    return report


def _decide_all(world, report: MaintenanceReport) -> None:
    for outcome in report.outcomes:
        ledger = world.state.content
        current = next(
            (c for c in ledger.conflicts if c.conflict_id == outcome.conflict_id), None
        )
        if current is None:
            outcome.result = "failed"
            outcome.error = "没有这条冲突记录"
            report.errors.append(f"{outcome.conflict_id}: 没有这条冲突记录")
            return
        if current.status is not ConflictStatus.PENDING:
            if current.status.value == outcome.requested and _in_current_cycle(ledger, current):
                # 磁盘上已经是这个决定（这次打开时从磁盘读出来的，不是上一个进程
                # 的内存）。不再记一次。
                outcome.result = "already"
                continue
            outcome.result = "failed"
            if current.status.value == outcome.requested:
                outcome.error = (
                    "这是一条历史记录，不是此刻这一轮的提议；用列表里此刻待决的那条 id"
                )
            else:
                outcome.error = f"已经被决定为 {current.status.value}，不会覆盖"
            report.errors.append(f"{outcome.conflict_id}: {outcome.error}")
            return
        try:
            world.runtime.decide_content(outcome.conflict_id, outcome.requested)
        except Exception as e:
            outcome.result = "failed"
            outcome.error = f"{type(e).__name__}: {e}"
            report.errors.append(f"{outcome.conflict_id}: {outcome.error}")
            return
        try:
            world.checkpoint("content_decision")
        except Exception as e:
            # 决定已经在内存里，可能还没（或者已经、但未经证实地）写下去。后面
            # 的一条都不再记；关闭时会再存一次，结果以关闭后的磁盘为准。存储失败、
            # 耐久未证实、所有权失败都走这里，保留各自的类别。
            outcome.result = "failed"
            outcome.error = f"决定之后保存失败: {type(e).__name__}: {e}"
            report.errors.append(f"{outcome.conflict_id}: {outcome.error}")
            return
        outcome.result = "recorded"


def _in_current_cycle(ledger: ContentLedger, conflict) -> bool:
    """一条已决记录是不是此刻这一轮的结果（重跑时才能算"已是此状态"）。

    采用：它就是定下此刻已采用版本的那一次。驳回 / 暂缓：它是此刻这个采用周期
    里的提议。更早周期的记录哪怕状态相同，也不是这一次要做的决定。
    """
    start = ledger.adoption_seq(conflict.subject)
    if conflict.status is ConflictStatus.ADOPTED:
        return conflict.decided_seq == start
    return conflict.offered_seq > start


__all__ = [
    "DECISIONS",
    "MAINTENANCE_POLICY",
    "ConflictView",
    "DecisionOutcome",
    "MaintenanceError",
    "MaintenanceReport",
    "read_conflicts",
    "run_content_decisions",
]
