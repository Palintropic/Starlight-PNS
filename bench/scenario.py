# bench/scenario.py — 场景即代码的加载与校验。
#
# 场景文件是 benchmark 的事实来源：换维度 = 换一个场景文件，harness 不动。
# 校验刻意严：场景里互相矛盾的声明（比如被问的人同时在观察者名单里）必须
# 在烧任何一次模型调用之前就炸出来，而不是跑完 120 次生成才发现设计错了。
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

BUILDERS = ("pns", "prompt_only", "prompt_rag")


class ScenarioError(ValueError):
    """场景文件不合法。消息必须说清是哪一条、为什么。"""


@dataclass
class RetrieverSpec:
    type: str
    tokenization: str
    k1: float
    b: float
    top_k: int
    fields: List[str]
    query: str

    def to_dict(self) -> Dict:
        return {
            "type": self.type,
            "tokenization": self.tokenization,
            "k1": self.k1,
            "b": self.b,
            "top_k": self.top_k,
            "fields": list(self.fields),
            "query": self.query,
        }


@dataclass
class ConditionSpec:
    id: str
    builder: str
    retriever: Optional[RetrieverSpec] = None

    def to_dict(self) -> Dict:
        out = {"id": self.id, "builder": self.builder}
        if self.retriever is not None:
            out["retriever"] = self.retriever.to_dict()
        return out


@dataclass
class Scenario:
    id: str
    benchmark: str
    dimension: str
    methodology_version: str
    world_id: str
    snapshot_revision: Optional[int]
    snapshot_sha256: Optional[str]
    placements: Optional[Dict[str, str]]
    model_name: str
    temperature: float
    max_tokens: int
    observers: List[str]
    probed: List[str]
    target_participants: List[str]
    target_visible_to: List[str]
    target_hidden_from: List[str]
    leak_markers: List[str]
    probe_question: Optional[str]
    probe_question_status: str
    valid_runs_per_character: int
    exclusions: List[Dict]
    conditions: List[ConditionSpec]
    raw: Dict
    path: Path
    file_sha256: str

    def condition(self, condition_id: str) -> ConditionSpec:
        for spec in self.conditions:
            if spec.id == condition_id:
                return spec
        raise ScenarioError(f"场景 '{self.id}' 没有条件 '{condition_id}'")

    @property
    def probe_question_is_placeholder(self) -> bool:
        return self.probe_question is None


def _str_list(value, where: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ScenarioError(f"{where} 必须是字符串列表")
    return list(value)


def load_scenario(path) -> Scenario:
    path = Path(path)
    if not path.exists():
        raise ScenarioError(f"场景文件不存在：{path}")
    blob = path.read_bytes()
    file_sha256 = hashlib.sha256(blob).hexdigest()
    try:
        raw = yaml.safe_load(blob.decode("utf-8"))
    except yaml.YAMLError as e:
        raise ScenarioError(f"场景文件不是合法 YAML：{e}") from e
    if not isinstance(raw, dict):
        raise ScenarioError("场景文件顶层必须是映射")

    for key in ("id", "benchmark", "dimension", "methodology_version"):
        if not raw.get(key):
            raise ScenarioError(f"场景缺少必填字段 '{key}'")

    world = raw.get("world") or {}
    if not world.get("id"):
        raise ScenarioError("world.id 必填")
    snapshot = world.get("snapshot") or {}
    snapshot_sha256 = snapshot.get("sha256")
    if snapshot_sha256 is not None:
        if (
            not isinstance(snapshot_sha256, str)
            or len(snapshot_sha256) != 64
            or any(c not in "0123456789abcdef" for c in snapshot_sha256.lower())
        ):
            raise ScenarioError("world.snapshot.sha256 必须是 64 位十六进制")

    model = raw.get("model") or {}
    if not model.get("name"):
        raise ScenarioError("model.name 必填")
    temperature = float(model.get("temperature", 0.85))
    max_tokens = int(model.get("max_tokens", 1024))
    if max_tokens <= 0:
        raise ScenarioError("model.max_tokens 必须为正")

    characters = raw.get("characters") or {}
    observers = _str_list(characters.get("observers"), "characters.observers")
    probed = _str_list(characters.get("probed"), "characters.probed")
    if not observers:
        raise ScenarioError("characters.observers 不能为空")
    if not probed:
        raise ScenarioError("characters.probed 不能为空")
    overlap = sorted(set(observers) & set(probed))
    if overlap:
        raise ScenarioError(
            f"被问角色不能同时是观察者（泄漏定义会失效）：{overlap}"
        )

    target = raw.get("target_event") or {}
    participants = _str_list(target.get("participants"), "target_event.participants")
    visible_to = _str_list(target.get("visible_to"), "target_event.visible_to")
    hidden_from = _str_list(target.get("hidden_from"), "target_event.hidden_from")
    if set(visible_to) != set(observers):
        raise ScenarioError(
            "target_event.visible_to 必须与 characters.observers 一致"
        )
    if not set(probed) <= set(hidden_from):
        raise ScenarioError("被问角色必须全部在 target_event.hidden_from 里")
    if set(participants) & set(probed):
        raise ScenarioError("被问角色不能是目标事件的参与者")

    probe = raw.get("probe") or {}
    question = probe.get("question")
    question_status = probe.get("question_status", "verbatim" if question else "")
    if question is None and question_status != "todo_from_artifact":
        raise ScenarioError(
            "probe.question 为空时 question_status 必须是 todo_from_artifact"
            "（占位必须显式声明，不许悄悄缺失）"
        )
    if question is not None and not isinstance(question, str):
        raise ScenarioError("probe.question 必须是字符串")

    runs = raw.get("runs") or {}
    valid_runs = int(runs.get("valid_per_character", 20))
    if valid_runs < 1:
        raise ScenarioError("runs.valid_per_character 至少为 1")

    conditions = []
    seen_ids = set()
    for entry in raw.get("conditions") or []:
        cid = entry.get("id")
        builder = entry.get("builder")
        if not cid or cid in seen_ids:
            raise ScenarioError(f"条件 id 缺失或重复：{cid!r}")
        seen_ids.add(cid)
        if builder not in BUILDERS:
            raise ScenarioError(
                f"条件 '{cid}' 的 builder 必须是 {BUILDERS} 之一，得到 {builder!r}"
            )
        retriever = None
        if builder == "prompt_rag":
            spec = entry.get("retriever") or {}
            if spec.get("type") != "bm25":
                raise ScenarioError(
                    f"条件 '{cid}' 目前只支持 bm25 检索器，得到 {spec.get('type')!r}"
                )
            if spec.get("tokenization") != "zh_char_ngram_1_2":
                raise ScenarioError(
                    f"条件 '{cid}' 目前只支持 zh_char_ngram_1_2 分词"
                )
            k1 = float(spec.get("k1", 1.5))
            b = float(spec.get("b", 0.75))
            top_k = int(spec.get("top_k", 5))
            if k1 <= 0:
                raise ScenarioError(f"条件 '{cid}' 的 retriever.k1 必须为正")
            if not 0.0 <= b <= 1.0:
                raise ScenarioError(f"条件 '{cid}' 的 retriever.b 必须在 [0, 1]")
            if top_k < 1:
                raise ScenarioError(f"条件 '{cid}' 的 retriever.top_k 至少为 1")
            retriever = RetrieverSpec(
                type="bm25",
                tokenization="zh_char_ngram_1_2",
                k1=k1,
                b=b,
                top_k=top_k,
                fields=_str_list(spec.get("fields"), "retriever.fields"),
                query=str(spec.get("query", "probe_question_only")),
            )
        conditions.append(
            ConditionSpec(id=cid, builder=builder, retriever=retriever)
        )
    if not conditions:
        raise ScenarioError("conditions 不能为空")

    return Scenario(
        id=str(raw["id"]),
        benchmark=str(raw["benchmark"]),
        dimension=str(raw["dimension"]),
        methodology_version=str(raw["methodology_version"]),
        world_id=str(world["id"]),
        snapshot_revision=snapshot.get("revision"),
        snapshot_sha256=snapshot_sha256,
        placements=world.get("placements"),
        model_name=str(model["name"]),
        temperature=temperature,
        max_tokens=max_tokens,
        observers=observers,
        probed=probed,
        target_participants=participants,
        target_visible_to=visible_to,
        target_hidden_from=hidden_from,
        leak_markers=_str_list(raw.get("leak_markers") or [], "leak_markers"),
        probe_question=question,
        probe_question_status=question_status,
        valid_runs_per_character=valid_runs,
        exclusions=list(raw.get("exclusions") or []),
        conditions=conditions,
        raw=raw,
        path=path,
        file_sha256=file_sha256,
    )
