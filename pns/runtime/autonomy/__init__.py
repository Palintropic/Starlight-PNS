# pns/runtime/autonomy — 自主运行时编排层
#
#     调度到期 → 角色作用域的 Agency 提案 → 生成 → Router 判分与审计
#              → 校验后的事件提交 → 曝光 / 观察 → 主观记忆 → 终局
#
# 这一层把 P4–P10 已经建好的各个权威接成一条回路。它**不**替换或复制其中
# 任何一个：调度、Agency、事件提交、曝光、记忆仍然各自守着自己的权威和事务
# 边界，协调器只负责按顺序把它们串起来，并且保证一次处理是一个事务。
#
# 这个包的初始化保持轻：只从子模块转出公开名字，不做任何 I/O、不读配置、
# 不初始化重载边界（有子进程测试盯着）。
from pns.models.authored import AuthoredTextError, GenerationAudit
from pns.runtime.autonomy.audit import (
    DEFAULT_THRESHOLD,
    AuditError,
    AuditRequest,
    LineAuditor,
    RouterAuditor,
    ScriptedAuditor,
)
from pns.runtime.autonomy.context import (
    CHARACTER_VISIBLE_PAYLOAD_KEYS,
    MAX_CUE_CHARS,
    ActivationCue,
    GenerationContext,
    GenerationContextError,
    build_generation_context,
)
from pns.runtime.autonomy.coordinator import AutonomousRuntime, AutonomyError
from pns.runtime.autonomy.generation import (
    MAX_LINE_CHARS,
    AuthoredLinePolicy,
    GenerationError,
    LineGenerator,
    ScriptedLineGenerator,
    first_authored_action,
    parse_line,
)
from pns.runtime.autonomy.outcome import (
    ActivationOutcome,
    ActivationResult,
    OutcomeError,
    RetryPolicy,
    outcome_for,
)

__all__ = [
    "CHARACTER_VISIBLE_PAYLOAD_KEYS",
    "DEFAULT_THRESHOLD",
    "MAX_CUE_CHARS",
    "MAX_LINE_CHARS",
    "ActivationCue",
    "ActivationOutcome",
    "ActivationResult",
    "AuditError",
    "AuditRequest",
    "AuthoredLinePolicy",
    "AuthoredTextError",
    "AutonomousRuntime",
    "AutonomyError",
    "GenerationAudit",
    "GenerationContext",
    "GenerationContextError",
    "GenerationError",
    "LineAuditor",
    "LineGenerator",
    "OutcomeError",
    "RetryPolicy",
    "RouterAuditor",
    "ScriptedAuditor",
    "ScriptedLineGenerator",
    "build_generation_context",
    "first_authored_action",
    "outcome_for",
    "parse_line",
]
