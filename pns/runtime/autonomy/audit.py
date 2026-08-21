# pns/runtime/autonomy/audit.py — Router 判分与生成审计
#
# 这一层回答的问题只有一个：**这句话，像不像本人说的？像到可以成为世界真相
# 的程度吗？**
#
# 它不回答：要不要动（Agency）、说什么（生成层）、谁能感知到（Exposure）、
# 记不记得住（Memory）。
#
# 三条硬约束：
#
#   1. **判定是推导出来的。** is_ooc 由分数与阈值现算（见
#      pns/models/authored.py），判分器自己交回来的那个布尔值根本没有存储
#      位置。一个说"我觉得没问题"的判分器，救不了一句 9 分的台词。
#   2. **凭据绑定到这一句。** 产出的 GenerationAudit 里带着逐字相同的原文、
#      提案身份与角色身份。换一句话再用同一份凭据，在提交那一刻就不成立。
#   3. **判分失败不是判分通过。** 判分器抛异常 → AuditError，由协调器按
#      重试预算处理；它绝不会退化成"那就当它通过吧"。旧的研究路径上，Router
#      调用失败会返回一份 drift_score=0 的兜底结果并继续跑（那条路上有人
#      看着屏幕）；自主路径上没人看着，所以这里一次都不允许那样退化。
#
# 判分器本身是注入进来的可调用对象：确定性的（ScriptedAuditor）和真实的
# （RouterAuditor + pns.logic.router.judge）走同一条通道，所以完整回路可以
# 不联网跑完，而结论对真实路径同样成立。这个模块自己不 import 任何 HTTP 或
# 模型 SDK —— 有测试盯着。
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Mapping, Optional

from pns.models.action import ActionId
from pns.models.authored import AuthoredTextError, GenerationAudit

# 默认漂移阈值。跟 pns.logic.router 的口径一致：达到阈值即视为 OOC。
DEFAULT_THRESHOLD = 5.0


class AuditError(ValueError):
    """这次判分没能给出一个结果。

    它跟"判成了不接受"是两类东西：后者是一份合法的 GenerationAudit，会被
    耐久地记下来；AuditError 是判分本身没发生 —— 判分器不可用、返回的形状
    根本不是判分结果。`retryable` 的含义与 GenerationError 相同。
    """

    def __init__(self, *args, retryable: bool = False):
        super().__init__(*args)
        self.retryable = bool(retryable)


@dataclass(frozen=True)
class AuditRequest:
    """交给判分器的那一份输入。

    刻意很窄：一句话、说它的角色、它要落成哪个动作、什么时候。判分是**系统
    过程**，不是角色经验，所以它不需要（也拿不到）这个角色的记忆或观察 ——
    多给它的每一样，都是以后被顺手写进 provenance、再泄漏出去的东西。
    """

    character_id: str
    proposal_id: str
    # 这条提案的完整 payload。判分器通常只看 text，但凭据要绑的是整份 ——
    # 否则"台词不动、换个显示名"就能沿用同一份凭据。
    payload: Mapping
    action_id: ActionId
    now: datetime
    target_id: Optional[str] = None
    # 可选的判分参考上下文：生成前这个角色自己看到的最近几行。它只进判分
    # 提示，不进任何事件、观察或记忆。
    recent_lines: tuple = ()

    @property
    def text(self) -> str:
        """被判的那句话。判分器要的就是它。"""
        return self.payload["text"]

    def to_dict(self) -> Dict:
        return {
            "character_id": self.character_id,
            "proposal_id": self.proposal_id,
            "payload": dict(self.payload),
            "text": self.text,
            "action_id": ActionId(self.action_id).value,
            "target_id": self.target_id,
            "now": self.now.isoformat(),
            "recent_lines": list(self.recent_lines),
        }


class LineAuditor:
    """判分器接口。`audit()` 要么交回一份 GenerationAudit，要么抛 AuditError。"""

    name = "auditor"

    def audit(self, request: AuditRequest) -> GenerationAudit:
        raise NotImplementedError


def _audit_from(
    request: AuditRequest,
    *,
    drift_score,
    threshold: float,
    evaluator_model: str = "",
    evaluator_provider: str = "",
    generator_model: str = "",
    generator_provider: str = "",
    confidence=0.0,
    needs_human_review: bool = False,
    dimensions: Optional[Mapping] = None,
    dimensions_complete: bool = False,
    methodology_version: str = "",
    router_reference_status: str = "",
) -> GenerationAudit:
    """构造凭据的唯一一处。形状不合法就是一次判分失败，不是一次通过。"""
    try:
        return GenerationAudit(
            proposal_id=request.proposal_id,
            character_id=request.character_id,
            payload=dict(request.payload),
            drift_score=drift_score,
            threshold=threshold,
            audited_at=request.now,
            evaluator_model=evaluator_model,
            evaluator_provider=evaluator_provider,
            generator_model=generator_model,
            generator_provider=generator_provider,
            methodology_version=methodology_version,
            router_reference_status=router_reference_status,
            confidence=confidence,
            needs_human_review=needs_human_review,
            dimensions=dimensions or {},
            dimensions_complete=dimensions_complete,
        )
    except AuthoredTextError as e:
        raise AuditError(f"判分结果不合法: {e}") from e


class ScriptedAuditor(LineAuditor):
    """确定性判分器：按台词查分，查不到就用默认分。

    存在的意义跟 ScriptedLineGenerator 一样 —— 让完整回路可以不联网跑完，
    而且每次跑出来逐字节相同。
    """

    name = "scripted"

    def __init__(
        self,
        scores: Optional[Mapping[str, float]] = None,
        *,
        default_score: float = 0.0,
        threshold: float = DEFAULT_THRESHOLD,
        needs_human_review: bool = False,
        evaluator_model: str = "scripted",
    ):
        if scores is not None and not isinstance(scores, Mapping):
            raise AuditError("scores 必须是字典")
        self._scores = dict(scores or {})
        self._default = default_score
        self._threshold = threshold
        self._needs_human_review = bool(needs_human_review)
        self._evaluator_model = evaluator_model

    def audit(self, request: AuditRequest) -> GenerationAudit:
        return _audit_from(
            request,
            drift_score=self._scores.get(request.text, self._default),
            threshold=self._threshold,
            evaluator_model=self._evaluator_model,
            needs_human_review=self._needs_human_review,
            confidence=1.0,
            dimensions_complete=True,
        )


class RouterAuditor(LineAuditor):
    """真实 Router 判分的适配器。

    它只做一件事：把一个外部判分器交回来的**原始形状**翻译成 GenerationAudit。
    它刻意不判断"这句话能不能提交" —— 那由凭据自己的 accepted 推导，而推导
    只看分数、阈值和人工复核标记。判分器交回来的 `is_ooc` 被**丢弃**：让
    被判的一方自己宣布自己合格，等于没有判分。

    构造只吃一个可调用对象：它拿不到 SessionState、拿不到世界、拿不到事件
    历史，所以它没有任何提交的能力，只有判定的能力。

    真实接线是 `RouterAuditor(lambda request: pns.logic.router.judge(...))`，
    在协调器之外完成 —— 这个模块不 import 任何模型 SDK。
    """

    name = "router"

    def __init__(
        self,
        judge: Callable[[AuditRequest], object],
        *,
        threshold: float = DEFAULT_THRESHOLD,
        evaluator_model: str = "",
        evaluator_provider: str = "",
        generator_model: str = "",
        generator_provider: str = "",
        name: Optional[str] = None,
    ):
        if not callable(judge):
            raise AuditError("Router 适配器需要一个可调用的判分器")
        self._judge = judge
        self._threshold = threshold
        self._evaluator_model = evaluator_model
        self._evaluator_provider = evaluator_provider
        self._generator_model = generator_model
        self._generator_provider = generator_provider
        if name is not None:
            if not isinstance(name, str) or not name:
                raise AuditError("name 必须是非空字符串")
            self.name = name

    def audit(self, request: AuditRequest) -> GenerationAudit:
        try:
            raw = self._judge(request)
        except AuditError:
            raise
        except Exception as e:
            # 判分器背后是网络和模型。**绝不**退化成"那就当它通过吧"。
            raise AuditError(
                f"判分器调用失败: {type(e).__name__}: {e}", retryable=True
            ) from e

        if not isinstance(raw, Mapping):
            raise AuditError(
                f"判分器必须返回字典，收到 {type(raw).__name__}", retryable=False
            )
        if "drift_score" not in raw:
            # 没有分数的判分结果不是"零分"，是根本没判。
            raise AuditError("判分结果里没有 drift_score", retryable=False)

        needs_review = bool(raw.get("needs_human_review", False))
        # 七维不全 = 判分器没能完整评估。研究路径上这会被标成待人工复核；
        # 自主路径上没人复核，所以它就是不接受。
        #
        # 字段缺失按"不全"处理，不按"完整"：缺失的意思是**不知道**，而在
        # "不知道"和"接受"之间选接受，正是这一层不允许的那种退化。真实
        # Router 调用失败时的兜底结果恰好长这样（0 分 + 不完整 + 待复核），
        # 而那份兜底在研究路径上只是记一笔，在这里必须拦住。
        complete = bool(raw.get("dimensions_complete", False))
        if not complete:
            needs_review = True
        return _audit_from(
            request,
            drift_score=raw.get("drift_score"),
            threshold=self._threshold,
            evaluator_model=raw.get("evaluator_model") or self._evaluator_model,
            evaluator_provider=(
                raw.get("evaluator_provider") or self._evaluator_provider
            ),
            generator_model=self._generator_model,
            generator_provider=self._generator_provider,
            confidence=raw.get("confidence", 0.0),
            needs_human_review=needs_review,
            dimensions=raw.get("dimensions") or {},
            dimensions_complete=complete,
            methodology_version=raw.get("methodology_version", "") or "",
            router_reference_status=raw.get("router_reference_status", "") or "",
        )


__all__ = [
    "DEFAULT_THRESHOLD",
    "AuditError",
    "AuditRequest",
    "LineAuditor",
    "RouterAuditor",
    "ScriptedAuditor",
]
