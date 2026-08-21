# pns/runtime — 会话编排层，从 WebSocket 传输层（pns.interfaces.simulate）里抽出来。
#
# 这个包的公开面就是下面这份清单：会话编排、事件提交边界、曝光判定、调度与
# 时间推进、配置构建与重载。调用方按名字从 pns.runtime 取，不该去记某个类
# 具体住在哪个文件里 —— 文件位置是这一层内部的事，重排文件不该震动调用方。
from pns.runtime.content_registry import (
    ConfigValidationError,
    ContentRegistry,
    build_content_registry,
)
from pns.runtime.event_commit import (
    EventCommitError,
    apply_event,
    commit_dialogue,
    commit_event,
    commit_session_event,
    dialogue_event_for_turn,
    project_turn_message,
    validate_against_world,
)
from pns.runtime.exposure import (
    ExposureRuleError,
    candidate_characters,
    evaluate_event_exposure,
    evaluate_exposure,
    explain_character,
    explain_event,
    observation_for,
    observations_for,
    perceived_content,
)
from pns.runtime.reload import (
    BOUNDARY,
    SUPERVISOR,
    ConfigBoundary,
    ReloadResult,
    SessionAdmissionClosed,
    SessionSupervisor,
    active_registry,
    write_and_reload,
)
from pns.runtime.scheduler import (
    PersistentScheduler,
    SchedulerError,
    TickResult,
)
from pns.runtime.session_runtime import (
    SessionRefusedError,
    SessionRuntime,
    SessionSetupError,
)

__all__ = [
    # 会话编排
    "SessionRuntime", "SessionSetupError", "SessionRefusedError",
    # 事件提交边界
    "commit_event", "commit_session_event", "commit_dialogue", "apply_event",
    "validate_against_world", "dialogue_event_for_turn", "project_turn_message",
    "EventCommitError",
    # 曝光
    "evaluate_exposure", "evaluate_event_exposure", "candidate_characters",
    "observation_for", "observations_for", "perceived_content",
    "explain_event", "explain_character", "ExposureRuleError",
    # 调度
    "PersistentScheduler", "TickResult", "SchedulerError",
    # 配置与重载
    "ContentRegistry", "build_content_registry", "ConfigValidationError",
    "ConfigBoundary", "ReloadResult", "SessionSupervisor",
    "SessionAdmissionClosed", "active_registry", "write_and_reload",
    "BOUNDARY", "SUPERVISOR",
]
