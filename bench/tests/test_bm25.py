import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.conditions.base import DialogueEvent
from bench.conditions.prompt_rag import BM25Retriever, tokenize_zh_ngram


def ev(speaker, name, text, **kw):
    return DialogueEvent(speaker=speaker, speaker_name=name, text=text, **kw)


class TokenizeTests(unittest.TestCase):
    def test_unigrams_and_bigrams(self):
        self.assertEqual(
            tokenize_zh_ngram("奏的曲"),
            ["奏", "的", "曲", "奏的", "的曲"],
        )

    def test_whitespace_removed_and_deterministic(self):
        self.assertEqual(tokenize_zh_ngram("奏 的 曲"), tokenize_zh_ngram("奏的曲"))
        self.assertEqual(tokenize_zh_ngram("星屑粉末"), tokenize_zh_ngram("星屑粉末"))


class BM25Tests(unittest.TestCase):
    def setUp(self):
        self.corpus = [
            ev("a", "甲", "今天天气不错，我们去散步吧"),
            ev("b", "乙", "副歌的意象我想用星星和月亮", is_target=True),
            ev("c", "丙", "我去便利店买东西了"),
            ev("d", "丁", "这首曲子的编曲改了三版"),
        ]

    def test_relevant_document_ranks_first(self):
        retriever = BM25Retriever(k1=1.5, b=0.75, top_k=3)
        hits = retriever.retrieve("副歌意象", self.corpus)
        self.assertEqual(hits[0].index, 1)
        self.assertEqual(hits[0].rank, 1)
        self.assertTrue(hits[0].event.is_target)

    def test_top_k_respected(self):
        retriever = BM25Retriever(top_k=2)
        self.assertEqual(len(retriever.retrieve("曲子", self.corpus)), 2)

    def test_deterministic_order(self):
        retriever = BM25Retriever(top_k=4)
        first = [(h.index, h.score) for h in retriever.retrieve("曲子", self.corpus)]
        second = [(h.index, h.score) for h in retriever.retrieve("曲子", self.corpus)]
        self.assertEqual(first, second)

    def test_empty_corpus(self):
        self.assertEqual(BM25Retriever().retrieve("任意", []), [])

    def test_invalid_parameters(self):
        with self.assertRaises(ValueError):
            BM25Retriever(k1=0)
        with self.assertRaises(ValueError):
            BM25Retriever(b=1.5)
        with self.assertRaises(ValueError):
            BM25Retriever(top_k=0)


if __name__ == "__main__":
    unittest.main()
