# pns/models/drift_score.py
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class DriftScore:
    """Router评分数据类"""
    session_id: str
    turn: int
    character: str
    drift_score: float          # 0-10，数值越高表示drift越严重
    confidence: float           # 0-1，Router对评分的置信度
    reason: str                 # 为什么认为有/没有drift
    needs_human_review: bool    # 是否需要人工审核
    drift_type: str = "none"    # none / type_a / type_b（语气漂移 / 任务执行漂移）
    scene_id: str = ""
    lore_tag: str = ""
    hardware_backend: str = ""
    dimensions: dict = field(default_factory=dict)
    dimensions_complete: bool = False
    methodology_version: str = ""
    timestamp: str = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()

    def to_dict(self):
        """转换为dict（用于JSON序列化）"""
        return {
            'session_id': self.session_id,
            'turn': self.turn,
            'character': self.character,
            'drift_score': self.drift_score,
            'confidence': self.confidence,
            'reason': self.reason,
            'needs_human_review': self.needs_human_review,
            'drift_type': self.drift_type,
            'scene_id': self.scene_id,
            'lore_tag': self.lore_tag,
            'hardware_backend': self.hardware_backend,
            'dimensions': self.dimensions,
            'dimensions_complete': self.dimensions_complete,
            'methodology_version': self.methodology_version,
            'timestamp': self.timestamp,
        }

    def is_problematic(self, threshold: float | None = None) -> bool:
        """判断是否超过drift阈值"""
        if threshold is None:
            from pns.logic.router import OOC_THRESHOLD
            threshold = OOC_THRESHOLD
        return self.drift_score >= threshold
