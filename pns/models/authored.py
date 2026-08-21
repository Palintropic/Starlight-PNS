# pns/models/authored.py — 一句台词被接受的凭据
#
# 这个模块只装一个类型：GenerationAudit。它回答的问题只有一个：
#
#     **这一句话，被判过分了吗？判的结果是接受吗？判的是不是就是这一句？**
#
# 在此之前，"需要外部提供台词的动作没有提交路径"是一条结构性的缺口（见
# pns/models/action.py 的 _require_committable）：生成 → Router 判分 → 审计
# 落盘 → 提交那条链还没接上，所以运行时里根本不存在把未判分台词写进世界
# 历史的代码。P11 把那条链接上了，于是缺口必须被替换成一道**闸**，而不是
# 一个布尔开关 —— 开关是调用方翻得动的，那样它就不是边界，只是一句建议。
#
# 这道闸的形状是：构造发言事件的那个唯一函数，要一份 GenerationAudit，而且
# 这份审计必须
#
#   1. 绑定到这条提案（提案 ID、角色 ID、以及**逐字相同**的那句话）；
#   2. 判定为接受（分数在阈值之下，且没有被标记为需要人工复核）。
#
# 于是"换一句话再用同一份审计"、"给别的角色借一份审计"、"分数超标但硬说
# 没超"这三种绕法，在类型层面就走不通。
#
# 为什么放在 models 而不是 runtime：审计要跟着 Agency 记录被序列化、被回滚、
# 被存档校验重新读出来核对，而存档校验住在 SessionState 里，models 不许
# import runtime。跟"资格规则必须能从观察本身重新推导"是同一条理由 ——
# 一个只有判分那一刻才知道的东西，恢复的时候就变成"存档说了算"。
#
# 判定是**推导**出来的，不是存进来的：is_ooc 由 drift_score 与 threshold 现算，
# 所以一个自称 is_ooc=False 的 Router 输出救不了一句超标的台词。判分器给的
# 那个布尔值根本没有存储位置。
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Mapping, Optional

from pns.models.frozen import freeze_json_value, thaw_json_value

# provenance / detail 里这份审计的类型标记。
AUDIT_KIND = "router_audit"

# 分数量表。Router 的七维评分与总分都在这个区间里，超出区间的分数不是
# "更严重"，而是这份结果根本不是这套方法论产出的。
MIN_SCORE = 0.0
MAX_SCORE = 10.0


class AuthoredTextError(ValueError):
    """审计记录本身不合法（空台词、分数越界、带时区的时间、字段损坏等）。"""


def _require_id(value, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AuthoredTextError(f"{label} 必须是非空字符串")
    return value


def _require_score(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AuthoredTextError(f"{label} 必须是数字，收到 {value!r}")
    value = float(value)
    if not MIN_SCORE <= value <= MAX_SCORE:
        raise AuthoredTextError(
            f"{label} 必须落在 {MIN_SCORE}–{MAX_SCORE} 之间，收到 {value}"
        )
    return value


@dataclass(frozen=True)
class GenerationAudit:
    """一次"这句话像不像本人"的判定结果。

    它是**审计**，不是世界真相：世界真相只有事件。这条记录说的是"这句话被
    哪个判分器、按哪个阈值、判成了什么"，包括判成不接受 —— 后者同样要留下
    记录，否则"判过但被拒"和"根本没判过"就分不出来，而这两者对下游是完全
    不同的事实。
    """

    proposal_id: str
    character_id: str
    # 被判的那条提案的**完整 payload**，逐字。绑定按它整份做，不只按那句话：
    # 一份只绑 text 的审计，挡不住"台词不动、把显示名换成别人"——而显示名
    # 正是别人观察到的"这是谁说的"。绑整份 payload，今天的 char_name 和以后
    # 新增的任何一个已声明键，都自动在凭据的覆盖范围里。
    payload: Mapping
    drift_score: float
    threshold: float
    audited_at: datetime
    evaluator_model: str = ""
    evaluator_provider: str = ""
    generator_model: str = ""
    generator_provider: str = ""
    methodology_version: str = ""
    router_reference_status: str = ""
    confidence: float = 0.0
    needs_human_review: bool = False
    dimensions: Mapping = field(default_factory=dict)
    dimensions_complete: bool = False

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "proposal_id", _require_id(self.proposal_id, "proposal_id"))
        set_(self, "character_id", _require_id(self.character_id, "character_id"))

        if not isinstance(self.payload, Mapping):
            raise AuthoredTextError("payload 必须是字典")
        text = self.payload.get("text")
        if not isinstance(text, str) or not text.strip():
            # 空台词判不出任何东西。一份"我判过一句空话"的审计，是给一条
            # 根本没生成出来的输出发通行证。
            raise AuthoredTextError("payload.text 必须是非空字符串")
        set_(
            self,
            "payload",
            freeze_json_value(self.payload, path="payload", error=AuthoredTextError),
        )

        set_(self, "drift_score", _require_score(self.drift_score, "drift_score"))
        set_(self, "threshold", _require_score(self.threshold, "threshold"))

        if not isinstance(self.audited_at, datetime):
            raise AuthoredTextError("audited_at 必须是 datetime（模拟时钟时间）")
        if self.audited_at.tzinfo is not None:
            raise AuthoredTextError(
                f"audited_at 必须是 timezone-naive 的模拟时间，收到 {self.audited_at!r}"
            )

        for name in (
            "evaluator_model",
            "evaluator_provider",
            "generator_model",
            "generator_provider",
            "methodology_version",
            "router_reference_status",
        ):
            if not isinstance(getattr(self, name), str):
                raise AuthoredTextError(f"{name} 必须是字符串")

        if isinstance(self.confidence, bool) or not isinstance(
            self.confidence, (int, float)
        ):
            raise AuthoredTextError("confidence 必须是数字")
        set_(self, "confidence", float(self.confidence))
        if not isinstance(self.needs_human_review, bool):
            raise AuthoredTextError("needs_human_review 必须是布尔值")
        if not isinstance(self.dimensions_complete, bool):
            raise AuthoredTextError("dimensions_complete 必须是布尔值")

        if not isinstance(self.dimensions, Mapping):
            raise AuthoredTextError("dimensions 必须是字典")
        set_(
            self,
            "dimensions",
            freeze_json_value(
                self.dimensions, path="dimensions", error=AuthoredTextError
            ),
        )

    @property
    def text(self) -> str:
        """被判的那句话。从 payload 推导，不另存一份 —— 两处说法迟早会不一致，
        而不一致的那一次就是"判的是 A、提交的是 B"。"""
        return self.payload["text"]

    def __hash__(self) -> int:
        # 冻结后的 payload/dimensions 不可哈希；审计的身份是"哪条提案的哪一句"。
        return hash((self.proposal_id, self.character_id, self.text))

    # ── 判定 ────────────────────────────────────────────────────────────
    @property
    def is_ooc(self) -> bool:
        """超过阈值就是漂移。**现算**，不存 —— 判分器自己说的那个布尔值
        没有存储位置，所以它说什么都改变不了这里的答案。"""
        return self.drift_score >= self.threshold

    @property
    def accepted(self) -> bool:
        """这句话可以成为世界真相吗。

        两个条件：没超阈值，而且判分器自己没有把它标成需要人工复核。
        后者是刻意的：自主路径上没有人盯着，一句连判分器都说"我拿不准"的
        台词直接写进世界历史，等于把不确定性当成了确定性。研究路径不同 ——
        那里有人在看着屏幕，OOC 的一轮会被记下来并触发纠正。
        """
        return not self.is_ooc and not self.needs_human_review

    def refusal(self) -> Optional[Dict]:
        """不被接受的原因；被接受就返回 None。"""
        if self.accepted:
            return None
        return {
            "drift_score": self.drift_score,
            "threshold": self.threshold,
            "is_ooc": self.is_ooc,
            "needs_human_review": self.needs_human_review,
        }

    # ── 绑定 ────────────────────────────────────────────────────────────
    def binds(self, proposal) -> bool:
        """这份审计判的就是这条提案吗。

        三样都要对上：提案身份、角色、以及**逐字相同的整份 payload**。
        少任何一样，"换一句话再用同一份审计"或者"台词不动、把显示名换成
        别人"就成立了 —— 后者在别人的观察里就是一次冒名。

        比的是解冻之后的普通结构：一边可能是只读视图，另一边可能是刚构造的
        字典，按引用或按类型比会把两份内容相同的 payload 判成不同。
        """
        payload = getattr(proposal, "payload", None)
        if not isinstance(payload, Mapping):
            return False
        return (
            getattr(proposal, "proposal_id", None) == self.proposal_id
            and getattr(proposal, "character_id", None) == self.character_id
            and thaw_json_value(payload) == thaw_json_value(self.payload)
        )

    def require_binding(self, proposal) -> None:
        if not self.binds(proposal):
            raise AuthoredTextError(
                f"审计记录 '{self.proposal_id}' 判的不是提案 "
                f"'{getattr(proposal, 'proposal_id', None)}' 的那份内容"
            )

    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> Dict:
        """完整公开形状；返回值是全新的可变结构，改它影响不到审计本身。

        `kind` 与 `is_ooc` 是**投影**，不是字段：读存档的人需要一眼看出这
        是什么、判成了什么，而 from_dict() 不读它们 —— 它们由字段现算，
        所以改存档里的 is_ooc 改不动任何判断。
        """
        return {
            "kind": AUDIT_KIND,
            "proposal_id": self.proposal_id,
            "character_id": self.character_id,
            "payload": thaw_json_value(self.payload),
            "drift_score": self.drift_score,
            "threshold": self.threshold,
            "is_ooc": self.is_ooc,
            "accepted": self.accepted,
            "audited_at": self.audited_at.isoformat(),
            "evaluator_model": self.evaluator_model,
            "evaluator_provider": self.evaluator_provider,
            "generator_model": self.generator_model,
            "generator_provider": self.generator_provider,
            "methodology_version": self.methodology_version,
            "router_reference_status": self.router_reference_status,
            "confidence": self.confidence,
            "needs_human_review": self.needs_human_review,
            "dimensions": thaw_json_value(self.dimensions),
            "dimensions_complete": self.dimensions_complete,
        }

    @classmethod
    def from_dict(cls, payload) -> "GenerationAudit":
        if not isinstance(payload, Mapping):
            raise AuthoredTextError("审计记录必须是字典")
        for required in (
            "proposal_id",
            "character_id",
            "payload",
            "drift_score",
            "threshold",
            "audited_at",
        ):
            if required not in payload:
                raise AuthoredTextError(f"审计记录缺少必填字段: {required}")
        audited_at = payload["audited_at"]
        if isinstance(audited_at, str):
            try:
                audited_at = datetime.fromisoformat(audited_at)
            except ValueError:
                raise AuthoredTextError(
                    f"无法解析的 audited_at: {payload['audited_at']!r}"
                ) from None
        return cls(
            proposal_id=payload["proposal_id"],
            character_id=payload["character_id"],
            payload=payload["payload"],
            drift_score=payload["drift_score"],
            threshold=payload["threshold"],
            audited_at=audited_at,
            evaluator_model=payload.get("evaluator_model", ""),
            evaluator_provider=payload.get("evaluator_provider", ""),
            generator_model=payload.get("generator_model", ""),
            generator_provider=payload.get("generator_provider", ""),
            methodology_version=payload.get("methodology_version", ""),
            router_reference_status=payload.get("router_reference_status", ""),
            confidence=payload.get("confidence", 0.0),
            needs_human_review=payload.get("needs_human_review", False),
            dimensions=payload.get("dimensions", {}),
            dimensions_complete=payload.get("dimensions_complete", False),
        )


__all__ = [
    "AUDIT_KIND",
    "AuthoredTextError",
    "GenerationAudit",
    "MAX_SCORE",
    "MIN_SCORE",
]
