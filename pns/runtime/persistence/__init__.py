# pns/runtime/persistence — 持久世界生命周期
#
#     创建或恢复 → 独占所有权 → 绑定运行时服务 → 运行 / checkpoint
#                → 停机并等事务落定 → 一份完整存档 → 归还所有权
#
# 这一层给一个自主世界一个完整的进程生命周期。它持久化的是**权威
# SessionState**：世界状态、事件历史、观察、曝光判定、排期与投递箱、Agency
# 审计、主观记忆。它不序列化任何活的东西 —— 服务实例、模型客户端、API Key、
# 锁、回调，一个都不进存档。
#
# 恢复边界只有一句：**恢复到最后一次成功的 checkpoint**。没有 WAL，没有事件
# 重放，没有零丢失崩溃保证。
#
# 这个包的初始化保持轻：只从子模块转出公开名字，不做任何 I/O、不建目录、
# 不拿任何锁、不初始化重载边界（有子进程测试盯着）。
from pns.runtime.persistence.archive import (
    WORLD_ARCHIVE_VERSION,
    ArchiveCorrupt,
    ArchiveError,
    WorldArchive,
)
from pns.runtime.persistence.lifecycle import (
    CheckpointError,
    CheckpointPolicy,
    LifecycleError,
    PersistentWorld,
    RuntimeAdapters,
    WorldLifecycleService,
)
from pns.runtime.persistence.naming import (
    MAX_WORLD_ID_LENGTH,
    WorldIdError,
    validate_world_id,
)
from pns.runtime.persistence.ownership import (
    OwnerRecord,
    OwnershipError,
    OwnershipHandle,
    OwnershipUnsupported,
    WorldAlreadyOwned,
    acquire_world,
    owned_world_paths,
)
from pns.runtime.persistence.store import (
    ArchiveNotDurable,
    ArchiveNotFound,
    FileWorldStore,
    SaveResult,
    StorageError,
    WorldStore,
)

__all__ = [
    "MAX_WORLD_ID_LENGTH",
    "WORLD_ARCHIVE_VERSION",
    "ArchiveCorrupt",
    "ArchiveError",
    "ArchiveNotDurable",
    "ArchiveNotFound",
    "CheckpointError",
    "CheckpointPolicy",
    "FileWorldStore",
    "LifecycleError",
    "OwnerRecord",
    "OwnershipError",
    "OwnershipHandle",
    "OwnershipUnsupported",
    "PersistentWorld",
    "RuntimeAdapters",
    "SaveResult",
    "StorageError",
    "WorldAlreadyOwned",
    "WorldArchive",
    "WorldIdError",
    "WorldLifecycleService",
    "WorldStore",
    "acquire_world",
    "owned_world_paths",
    "validate_world_id",
]
