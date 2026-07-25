# pns/models/session.py
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from datetime import datetime

@dataclass
class Turn:
    """单个turn的数据"""
    turn_number: int
    character: str
    prompt: str
    response: str
    timestamp: str
    
    def to_dict(self):
        return {
            'turn': self.turn_number,
            'character': self.character,
            'prompt': self.prompt,
            'response': self.response,
            'timestamp': self.timestamp,
        }

@dataclass
class SessionState:
    """Session完整状态"""
    session_id: str
    scene: str
    characters: List[str]              # 参与的角色列表
    turns: List[Turn] = field(default_factory=list)
    world_state: Dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    status: str = 'active'              # active / completed / paused
    metadata: Dict = field(default_factory=dict)
    
    def add_turn(self, turn: Turn):
        """添加一个turn"""
        self.turns.append(turn)
    
    def get_conversation(self) -> str:
        """获取完整的对话文本（用于显示）"""
        lines = []
        for turn in self.turns:
            lines.append(f"{turn.character}: {turn.response}")
        return "\n".join(lines)
    
    def to_dict(self):
        return {
            'session_id': self.session_id,
            'scene': self.scene,
            'characters': self.characters,
            'turns': [turn.to_dict() for turn in self.turns],
            'world_state': self.world_state,
            'created_at': self.created_at,
            'status': self.status,
            'metadata': self.metadata,
        }
