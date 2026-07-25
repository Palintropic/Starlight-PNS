# pns/models/world_state.py
from dataclasses import dataclass, field
from typing import Dict, List

@dataclass
class WorldState:
    """世界状态快照"""
    current_scene: str                          # 当前场景ID
    time: str                                   # 模拟时间 HH:MM
    character_locations: Dict[str, str] = field(default_factory=dict)  # {'绘名': 'Nightcord_Base'}
    active_characters: List[str] = field(default_factory=list)         # 当前在线角色
    world_facts_applied: str = ""               # 应用的WORLD_FACTS版本
    metadata: Dict = field(default_factory=dict)
    
    def is_character_available(self, character: str) -> bool:
        """检查角色是否在当前场景"""
        return character in self.active_characters
    
    def get_character_location(self, character: str) -> str:
        """获取角色的位置"""
        return self.character_locations.get(character, "unknown")
    
    def advance_time(self, minutes: int = 10):
        """推进模拟时间"""
        hour, minute = map(int, self.time.split(':'))
        minute += minutes
        if minute >= 60:
            hour += minute // 60
            minute = minute % 60
            if hour >= 24:
                hour = hour % 24
        self.time = f"{hour:02d}:{minute:02d}"
    
    def to_dict(self):
        return {
            'current_scene': self.current_scene,
            'time': self.time,
            'character_locations': self.character_locations,
            'active_characters': self.active_characters,
            'world_facts_applied': self.world_facts_applied,
            'metadata': self.metadata,
        }
