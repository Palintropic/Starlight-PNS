# bench/conditions/base.py — 三个条件共用的语料形状与消息渲染。
#
# 语料的单位是 dialogue.spoken 事件（谁、在哪、说了什么）。条件 A 把它整份
# 摊开，条件 B 拿它做检索，条件 C 把它逐条提交进会话状态让曝光投影去筛；
# 三份代码读的是同一份语料对象，不许各抄一份、各改一版。
import hashlib
import json
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class DialogueEvent:
    """语料里的一条发言。is_target 标记目标（隐藏）事件，供报告统计
    检索命中率用；组装上下文时不许拿它做过滤——那会改变被测架构本身。"""

    speaker: str
    speaker_name: str
    text: str
    location_id: Optional[str] = None
    channel_id: Optional[str] = None
    is_target: bool = False

    def to_dict(self) -> Dict:
        return {
            "speaker": self.speaker,
            "speaker_name": self.speaker_name,
            "text": self.text,
            "location_id": self.location_id,
            "channel_id": self.channel_id,
            "is_target": self.is_target,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "DialogueEvent":
        return cls(
            speaker=data["speaker"],
            speaker_name=data["speaker_name"],
            text=data["text"],
            location_id=data.get("location_id"),
            channel_id=data.get("channel_id"),
            is_target=bool(data.get("is_target", False)),
        )


def render_line(event: DialogueEvent) -> str:
    """一行转录。与运行时 record_turn 的遗留投影同形状：`名字：台词`。"""
    return f"{event.speaker_name}：{event.text}"


def scene_message(trigger: str) -> Dict:
    """与 SessionState.initialize_runtime 给每个角色的第一条消息同形状。"""
    return {"role": "user", "content": f"【场景】{trigger}"}


def question_message(question: str) -> Dict:
    return {"role": "user", "content": question}


def load_corpus_jsonl(path) -> List[DialogueEvent]:
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(DialogueEvent.from_dict(json.loads(line)))
    return events


def corpus_sha256(corpus: List[DialogueEvent]) -> str:
    payload = "\n".join(
        json.dumps(e.to_dict(), ensure_ascii=False, sort_keys=True) for e in corpus
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
