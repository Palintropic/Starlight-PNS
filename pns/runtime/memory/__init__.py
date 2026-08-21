# pns/runtime/memory — 主观记忆层
#
#     事件真相 → 曝光资格 → 观察投影 → **记忆编码** → **召回投影**
#                                              ↓
#                                        提示词里的那几行
#
# 这一层走两步，而且这两步是分开的：编码回答"留下了什么"，召回回答"此刻想起
# 什么"。存下来的记忆不会因为提示词换个问法而被改写。
#
# 它不判断世界上发生了什么（事件）、谁能感知到（曝光）、要不要行动（Agency）、
# 说出来像不像本人（Router）。
#
# 这个包的初始化保持轻：只从子模块转出公开名字，不做任何 I/O、不读配置、
# 不初始化重载边界（有子进程测试盯着）。
from pns.models.memory import (
    ClassBehavior,
    MemoryClass,
    MemoryError,
    MemoryMismatch,
    MemoryRecord,
    MemoryStore,
    derive_memory_id,
    derived_salience,
    describe_observation,
    eligible_classes,
    memory_content,
    memory_fragment,
    verify_memory_against_observation,
    world_fact,
)
from pns.runtime.memory.encoder import MemoryEncoder, MemoryEncoderError
from pns.runtime.memory.encoding import (
    EncodingDecision,
    EncodingError,
    EncodingOutcome,
    EncodingSignals,
    MemoryBudget,
    MemoryDraft,
    draft_memories,
    read_signals,
)
from pns.runtime.memory.projection import (
    prompt_block,
    prompt_projection,
    recalled_lines,
)
from pns.runtime.memory.recall import (
    MemoryRecall,
    RecallBudget,
    RecallError,
    RecallQuery,
    RecallResult,
    ScoredMemory,
    recall,
    score_memory,
)

__all__ = [
    "ClassBehavior",
    "EncodingDecision",
    "EncodingError",
    "EncodingOutcome",
    "EncodingSignals",
    "MemoryBudget",
    "MemoryClass",
    "MemoryDraft",
    "MemoryEncoder",
    "MemoryEncoderError",
    "MemoryError",
    "MemoryMismatch",
    "MemoryRecall",
    "MemoryRecord",
    "MemoryStore",
    "RecallBudget",
    "RecallError",
    "RecallQuery",
    "RecallResult",
    "ScoredMemory",
    "derive_memory_id",
    "derived_salience",
    "describe_observation",
    "draft_memories",
    "eligible_classes",
    "memory_content",
    "memory_fragment",
    "prompt_block",
    "prompt_projection",
    "read_signals",
    "recall",
    "recalled_lines",
    "score_memory",
    "verify_memory_against_observation",
    "world_fact",
]
