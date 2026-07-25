# world.py — 向后兼容薄封装
# PNS v0.2.0 — 实际内容已迁移至 pns/world/
# server.py / run.py 可继续从这里 import，无需修改

from pns.world import (
    WORLD_FACTS,
    SCENES,
    DEFAULT_SCENE,
    get_world_state_str,
    get_ena_system,
    get_mzk_system,
    get_ena_system_compat,
    get_mzk_system_compat,
    get_shizuku_system,
)

__all__ = [
    'WORLD_FACTS', 'SCENES', 'DEFAULT_SCENE',
    'get_world_state_str',
    'get_ena_system', 'get_mzk_system',
    'get_ena_system_compat', 'get_mzk_system_compat',
    'get_shizuku_system',
]
