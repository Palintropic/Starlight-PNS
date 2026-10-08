# tests/test_usage_meter.py — 用量计量的接线（COST-1）
#
# 守的线：
#   1. 生成、判分两条路都记账，记的模型名来自配置而不是响应；
#   2. 计量挂在 SDK 调用点：判分时 provider 抛 402，router.judge 会把它吞成
#      固定结果，但账本里照样是 failure=payment；
#   3. HTTP 200 但没有可用结果（没文本、判分不是 JSON）记 unusable，不让
#      连续失败归零；截断记 truncated；
#   4. 计量对调用结果透明：同一个响应原样返回，同一个异常原样抛出；
#   5. 账本写不进去时世界照常拿到结果；
#   6. 账本和汇总里没有异常或响应带来的任何字符串。
#
# 运行: python -m unittest discover -s tests -p test_usage_meter.py
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import anthropic  # noqa: E402
import httpx2  # noqa: E402

from pns.interfaces.composition import WorldControlPlane  # noqa: E402
from pns.models.action import ActionId  # noqa: E402
from pns.runtime.autonomy.audit import AuditRequest  # noqa: E402
from pns.runtime.autonomy.generation import GenerationError  # noqa: E402
from pns.runtime.reload import BOUNDARY  # noqa: E402
from pns.runtime import usage_ledger as ul  # noqa: E402

KEY = "sk-meter-canary-0123456789"
ROUTER_MARK = "监督者Router"
VERDICT = json.dumps(
    {"drift_score": 1, "confidence": 0.9, "drift_type": "无", "reason": "ok", "dimensions": {}},
    ensure_ascii=False,
)


class _Block:
    def __init__(self, text):
        self.text = text


def _response(text, *, usage=None, stop_reason="end_turn", model=KEY):
    return SimpleNamespace(
        content=[_Block(text)] if text is not None else [],
        stop_reason=stop_reason,
        usage=usage,
        model=model,  # 响应里的 model 字段不可信，不许进账本
    )


def _usage(fresh=120, cached=3000, out=40):
    return SimpleNamespace(
        input_tokens=fresh, output_tokens=out,
        cache_read_input_tokens=cached, cache_creation_input_tokens=None,
    )


def _status_error(code):
    response = httpx2.Response(code, request=httpx2.Request("POST", "http://x"))
    error = anthropic.APIStatusError(KEY, response=response, body={"error": {"message": KEY}})
    return error


class _Messages:
    def __init__(self, provider):
        self._provider = provider

    def create(self, *, system="", **kwargs):
        kind = "judge" if ROUTER_MARK in system else "generate"
        behaviour = self._provider.behaviour[kind]
        if isinstance(behaviour, BaseException):
            raise behaviour
        return behaviour


class FakeClient:
    def __init__(self):
        self.messages = _Messages(self)
        self.behaviour = {
            "generate": _response("今天也在这里哦", usage=_usage()),
            "judge": _response(VERDICT, usage=_usage(fresh=500, cached=0, out=80)),
        }
        self.other = "passthrough"


class MeterWiringTest(unittest.TestCase):
    def setUp(self):
        self.registry = BOUNDARY.active()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = patch.dict(os.environ, {self.registry.models.key_name: KEY})
        env.start()
        self.addCleanup(env.stop)
        self.client = FakeClient()
        self.ledger = ul.UsageLedger(
            self.tmp / "usage", clock=lambda: datetime(2026, 10, 8, 14, tzinfo=timezone.utc)
        )
        self.plane = WorldControlPlane(
            root=self.tmp / "worlds",
            client_factory=lambda *a, **k: self.client,
            usage_ledger=self.ledger,
        )
        adapters = self.plane.build_adapters(self.registry, world_id="yoake-mae")
        self.judge = adapters.auditor._judge
        self.call_model = self._call_model_of(self.plane, adapters)

    def _call_model_of(self, plane, adapters):
        state = plane.new_session_state(
            world_id="w", scene_id=self.registry.default_scene,
            character_ids=["kanade"], registry=self.registry,
        )
        return adapters.policy_factory(state)._generator._call

    def generate(self):
        return self.call_model("kanade", self.registry.scene(None), [{"role": "user", "content": "在吗"}])

    def audit(self):
        return self.judge(
            AuditRequest(
                character_id="mafuyu", proposal_id="p1", payload={"text": "嗯。"},
                action_id=ActionId.SPEAK_HERE,
                now=datetime(2026, 10, 8, 23, tzinfo=timezone.utc),
            )
        )

    def rows(self):
        records, corrupt = self.ledger.read()
        self.assertEqual(corrupt, 0)
        return records

    def test_generation_and_judge_are_both_recorded(self):
        self.generate()
        self.audit()
        gen, judge = self.rows()
        models = self.registry.models
        self.assertEqual((gen.path, gen.character_id, gen.world_id), ("generation", "kanade", "yoake-mae"))
        self.assertEqual((judge.path, judge.character_id), ("judge", "mafuyu"))
        self.assertEqual(gen.model, models.generator_model)
        self.assertEqual(judge.model, models.evaluator_model)
        self.assertEqual(gen.usage, ul.Usage(120, 0, 3000, 40))
        self.assertEqual((gen.operation, judge.operation), ("ok", "ok"))
        self.assertIsNotNone(gen.cost_yuan)

    def test_judge_payment_failure_is_seen_although_router_swallows_it(self):
        self.client.behaviour["judge"] = _status_error(402)
        result = self.audit()
        self.assertEqual(result["drift_type"], "error")  # router.judge 自己吞掉了
        (row,) = self.rows()
        self.assertEqual(
            (row.transport, row.failure, row.operation, row.cost_yuan),
            ("error", "payment", "failed", None),
        )

    def test_generation_failure_reraises_the_same_exception(self):
        error = _status_error(402)
        self.client.behaviour["generate"] = error
        with self.assertRaises(Exception) as caught:
            self.generate()
        self.assertIs(caught.exception, error)
        (row,) = self.rows()
        self.assertEqual((row.failure, row.operation), ("payment", "failed"))

    def test_http_200_without_text_is_unusable_and_does_not_reset(self):
        self.client.behaviour["generate"] = _status_error(402)
        with self.assertRaises(Exception):
            self.generate()
        self.client.behaviour["generate"] = _response(None, usage=_usage())
        with self.assertRaises(Exception):
            self.generate()
        self.client.behaviour["judge"] = _response("不是 JSON", usage=_usage())
        self.audit()
        rows = self.rows()
        self.assertEqual([r.operation for r in rows], ["failed", "unusable", "unusable"])
        self.assertEqual(ul.failure_streak(rows)["consecutive"], 3)

    def test_truncation_is_recorded_and_billed(self):
        self.client.behaviour["generate"] = _response("说到一半", usage=_usage(), stop_reason="max_tokens")
        with self.assertRaises(GenerationError):
            self.generate()
        (row,) = self.rows()
        self.assertEqual(row.operation, "truncated")
        self.assertIsNotNone(row.cost_yuan)

    def test_response_passes_through_unchanged(self):
        self.assertEqual(self.generate(), "今天也在这里哦")
        self.assertEqual(self.audit()["drift_type"], "无")

    def test_unwritable_ledger_does_not_change_results(self):
        blocker = self.tmp / "blocked"
        blocker.write_text("x")
        plane = WorldControlPlane(
            root=self.tmp / "worlds2",
            client_factory=lambda *a, **k: self.client,
            usage_ledger=ul.UsageLedger(blocker / "usage"),
        )
        adapters = plane.build_adapters(self.registry, world_id="w")
        call_model = self._call_model_of(plane, adapters)
        with patch("sys.stderr"):
            text = call_model("kanade", self.registry.scene(None), [{"role": "user", "content": "在吗"}])
        self.assertEqual(text, "今天也在这里哦")
        self.assertEqual(plane.usage_ledger.write_failures, 1)

    def test_nothing_from_the_provider_reaches_the_ledger(self):
        self.generate()
        self.client.behaviour["judge"] = _status_error(402)
        self.audit()
        hostile = type(KEY, (RuntimeError,), {"status_code": 402})(KEY)
        self.client.behaviour["generate"] = hostile
        with self.assertRaises(Exception):
            self.generate()
        for path in (self.tmp / "usage").glob("*.jsonl"):
            self.assertNotIn(KEY, path.read_text())
        summary = ul.summarize(self.ledger, days=1, anchor=None)
        self.assertNotIn(KEY, json.dumps(summary))
        self.assertEqual(self.rows()[-1].failure, "unknown")

    def test_other_client_attributes_pass_through(self):
        from pns.interfaces.usage_meter import MeteredClient, UsageMeter

        meter = UsageMeter(self.ledger, world_id="w", protocol="anthropic", prices={})
        wrapped = MeteredClient(self.client, meter)
        self.assertEqual(wrapped.other, "passthrough")
        # 没有 operation 上下文的调用照常放行、不记账。
        wrapped.messages.create(system="", messages=[])
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
