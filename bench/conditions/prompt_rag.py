# bench/conditions/prompt_rag.py — 条件 B：Prompt + 共享记忆检索。
#
# 检索器是可替换接口：KL-001 的 BM25（中文字符 1/2-gram、k1=1.5、b=0.75）
# 是参考实现。语料是**全局共享**的，没有角色级可见性过滤——首轮结果
# （泄漏 100%）测的就是这种架构；带感知过滤的检索是另一种架构，将来
# 新增一个 Retriever 实现即可，不改条件组装。
#
# 检索只许用探针问题本身作 query（场景里 query: probe_question_only）：
# 往 query 里塞目标事件的关键词，等于替被测系统作弊，首轮明确没这么做。
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from bench.conditions.base import (
    DialogueEvent,
    question_message,
    render_line,
    scene_message,
)


def tokenize_zh_ngram(text: str) -> List[str]:
    """确定性中文字符 1/2-gram：去掉空白后，每个单字是一个 token，
    每对相邻字是一个 token。不分词、不依赖词典，同一输入永远同一输出。"""
    chars = [c for c in text if not c.isspace()]
    grams = list(chars)
    grams.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return grams


@dataclass(frozen=True)
class RetrievalHit:
    index: int
    score: float
    rank: int  # 从 1 开始
    event: DialogueEvent

    def to_dict(self) -> Dict:
        return {
            "index": self.index,
            "rank": self.rank,
            "score": round(self.score, 6),
            "speaker": self.event.speaker,
            "is_target": self.event.is_target,
        }


class Retriever(ABC):
    """检索器接口。实现必须确定性：同一语料、同一 query、同一参数，
    返回同一排序——benchmark 的检索环节不许有随机性。"""

    @abstractmethod
    def retrieve(
        self, query: str, corpus: Sequence[DialogueEvent]
    ) -> List[RetrievalHit]:
        ...

    @abstractmethod
    def config(self) -> Dict:
        ...


class BM25Retriever(Retriever):
    """Okapi BM25，idf 用 Robertson 的正值形式 ln(1 + (N-df+.5)/(df+.5))，
    避免小语料上出现负 idf 把常见词排成负贡献。文档文本 = 场景声明的
    fields 拼接（KL-001 是 speaker_name + text）。"""

    def __init__(
        self,
        *,
        k1: float = 1.5,
        b: float = 0.75,
        top_k: int = 5,
        fields: Sequence[str] = ("speaker_name", "text"),
    ):
        if k1 <= 0:
            raise ValueError("k1 必须为正")
        if not 0.0 <= b <= 1.0:
            raise ValueError("b 必须在 [0, 1]")
        if top_k < 1:
            raise ValueError("top_k 至少为 1")
        self.k1 = float(k1)
        self.b = float(b)
        self.top_k = int(top_k)
        self.fields = tuple(fields)

    def config(self) -> Dict:
        return {
            "type": "bm25",
            "tokenization": "zh_char_ngram_1_2",
            "k1": self.k1,
            "b": self.b,
            "top_k": self.top_k,
            "fields": list(self.fields),
        }

    def _document(self, event: DialogueEvent) -> str:
        return " ".join(str(getattr(event, f)) for f in self.fields)

    def retrieve(
        self, query: str, corpus: Sequence[DialogueEvent]
    ) -> List[RetrievalHit]:
        if not corpus:
            return []
        docs = [tokenize_zh_ngram(self._document(e)) for e in corpus]
        lengths = [len(d) for d in docs]
        avgdl = sum(lengths) / len(lengths) if lengths else 0.0
        df: Dict[str, int] = {}
        for doc in docs:
            for term in set(doc):
                df[term] = df.get(term, 0) + 1
        n = len(docs)
        query_terms = tokenize_zh_ngram(query)

        scored = []
        for index, doc in enumerate(docs):
            tf: Dict[str, int] = {}
            for term in doc:
                tf[term] = tf.get(term, 0) + 1
            dl = lengths[index]
            norm = (1.0 - self.b + self.b * (dl / avgdl)) if avgdl else 1.0
            score = 0.0
            for term in query_terms:
                f = tf.get(term, 0)
                if not f:
                    continue
                idf = math.log(1.0 + (n - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * (f * (self.k1 + 1.0)) / (f + self.k1 * norm)
            scored.append((index, score))
        # 分数降序；同分按语料顺序（index 升序）——排序必须完全确定性。
        scored.sort(key=lambda item: (-item[1], item[0]))
        hits = []
        for rank, (index, score) in enumerate(scored[: self.top_k], start=1):
            hits.append(
                RetrievalHit(
                    index=index, score=score, rank=rank, event=corpus[index]
                )
            )
        return hits


def retriever_from_spec(spec) -> BM25Retriever:
    return BM25Retriever(k1=spec.k1, b=spec.b, top_k=spec.top_k, fields=spec.fields)


def build_history(
    corpus: List[DialogueEvent],
    character: str,
    question: str,
    retriever: Retriever,
    *,
    trigger: str = "",
) -> Tuple[List[Dict], Dict]:
    """history = 场景 + 一块"检索到的相关记忆" + 探针问题。

    检索命中的事件原文整行进上下文（与首轮一致：命中即原文呈现），
    命中清单（index/rank/score/是否目标事件）回给运行器记进 run record，
    报告要靠它回答"目标事件被检索到了吗、排第几"。"""
    hits = retriever.retrieve(question, corpus)
    history: List[Dict] = []
    if trigger:
        history.append(scene_message(trigger))
    memory_block = "\n".join(render_line(hit.event) for hit in hits)
    history.append({"role": "user", "content": f"【检索到的相关记忆】\n{memory_block}"})
    history.append(question_message(question))
    info = {"retrieved": [hit.to_dict() for hit in hits]}
    return history, info
