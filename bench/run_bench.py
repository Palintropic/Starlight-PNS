# bench/run_bench.py — BENCH-1 运行器。
#
#   python bench/run_bench.py --scenario kl-001 --mock --runs 3
#   python bench/run_bench.py --scenario kl-001 --corpus events.jsonl \
#       --condition pns,prompt_rag --model mimo-v2.6-pro --judge
#
# 一次运行 = 一个输出目录（默认 data/bench/<scenario>/<run-id>/）：
# records.jsonl（逐次尝试，含失败）+ manifest.json（provenance）。
# 报告由 bench/report.py 从输出目录生成，两者解耦：标注可以事后补，
# 报告随时重算。
#
# 生成只走 pns.logic.simulation.call_character —— 全仓唯一的 provider
# 分支。bench 不许自己拼请求：多一条生成路径，三个条件就不再可比。
import argparse
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench import fixtures
from bench.conditions import pns as pns_condition
from bench.conditions import prompt_only, prompt_rag
from bench.conditions.base import corpus_sha256, load_corpus_jsonl
from bench.labels import auto_label
from bench.metering import MeteringClient
from bench.scenario import Scenario, load_scenario
from pns.logic.simulation import GenerationTruncated, call_character

MAX_ATTEMPT_FACTOR = 3  # 有效数 ×3 封顶：provider 持续失败时不许无限烧


class BenchRunError(RuntimeError):
    pass


# ── 输入解析 ────────────────────────────────────────────────────────
def resolve_inputs(args, scenario: Scenario) -> Dict:
    """决定这次运行的语料、问题、标记、落位。真实模式缺一手材料就拒绝
    运行并说明缺什么——拿夹具数字冒充基线，是 benchmark 最不能犯的错。"""
    if args.corpus:
        corpus = load_corpus_jsonl(args.corpus)
        corpus_source = f"file:{args.corpus}"
        question = scenario.probe_question
        markers = scenario.leak_markers
        placements = scenario.placements
        clock = fixtures.FIXTURE_CLOCK
        trigger = ""
        if question is None:
            raise BenchRunError(
                "场景的探针问题还是 TODO 占位（probe.question 为 null）："
                "请先从 KL-001 artifact 逐字转录填入场景文件再跑真实模式"
            )
        if placements is None:
            raise BenchRunError(
                "场景没有声明 placements：真实快照的落位需要从快照恢复"
                "（后续决定，见 bench/README），或在场景里显式声明静态落位"
            )
    elif args.mock:
        corpus = list(fixtures.CORPUS)
        corpus_source = "synthetic_fixture"
        question = fixtures.FIXTURE_QUESTION
        markers = list(fixtures.LEAK_MARKERS)
        placements = dict(fixtures.PLACEMENTS)
        clock = fixtures.FIXTURE_CLOCK
        trigger = fixtures.FIXTURE_TRIGGER
    else:
        raise BenchRunError(
            "真实模式需要 --corpus <events.jsonl>（从固定快照导出的 "
            "dialogue.spoken 语料）；快照的直接恢复是后续决定（bench/README）。"
            "只想冒烟管线请用 --mock"
        )
    if not corpus:
        raise BenchRunError("语料为空")
    if not any(e.is_target for e in corpus):
        raise BenchRunError(
            "语料里没有 is_target=true 的目标事件：泄漏无从判定，拒绝运行"
        )
    return {
        "corpus": corpus,
        "corpus_source": corpus_source,
        "question": question,
        "leak_markers": markers,
        "placements": placements,
        "clock": clock,
        "trigger": trigger,
    }


def subject_terms(scenario: Scenario, corpus) -> List[str]:
    """自动轨判 unsupported_claim 用的主体词：目标事件参与者的 id 与语料
    里出现过的说话者名。"""
    terms = set(scenario.target_participants)
    for event in corpus:
        if event.speaker in scenario.target_participants:
            terms.add(event.speaker_name)
    return sorted(terms)


# ── Registry / client ───────────────────────────────────────────────
def build_registry(model_name: Optional[str]):
    """与 scripts/model1_compare.py 同一条路：服务器侧内容快照，
    --model 只换生成线路的模型名，其余配置（线路/凭据/价目）不动。"""
    from dotenv import load_dotenv

    from pns.runtime.content_registry import ENV_PATH, build_content_registry

    load_dotenv(ENV_PATH, override=True)
    registry = build_content_registry()
    if model_name:
        models = registry.models
        models = replace(
            models, generation=replace(models.generation, model=model_name)
        )
        registry = replace(registry, models=models)
    return registry


def git_info() -> Dict:
    def _git(*argv):
        try:
            return subprocess.run(
                ["git", *argv], cwd=ROOT, capture_output=True, text=True, timeout=10
            ).stdout.strip()
        except Exception:
            return ""

    return {
        "commit": _git("rev-parse", "HEAD") or None,
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD") or None,
        "dirty": bool(_git("status", "--porcelain")),
    }


# ── 单条生成 ────────────────────────────────────────────────────────
def run_attempts(
    *,
    scenario: Scenario,
    spec,
    character: str,
    history: List[Dict],
    info: Dict,
    context,
    client: MeteringClient,
    registry,
    model: str,
    prices,
    valid_target: int,
    terms: List[str],
    markers: List[str],
    judge_client: Optional[MeteringClient],
    judge: bool,
    run_id: str,
) -> List[Dict]:
    """跑到 valid_target 条有效生成为止；失败按场景的排除口径记账后补跑。"""
    records: List[Dict] = []
    valid = 0
    attempt = 0
    while valid < valid_target and attempt < valid_target * MAX_ATTEMPT_FACTOR:
        attempt += 1
        calls_before = client.call_count
        started = time.perf_counter()
        failure = None
        answer = None
        try:
            answer = call_character(
                client,
                character,
                history,
                context,
                model,
                scenario.max_tokens,
                scenario.temperature,
                registry=registry,
            )
        except GenerationTruncated:
            failure = "GenerationTruncated"
        except Exception as e:  # provider/解析失败：只记类名（文本可能带凭据）
            failure = type(e).__name__
        latency_ms = (time.perf_counter() - started) * 1000.0
        new_calls = client.calls[calls_before:]
        usage = None
        cost = None
        if new_calls and new_calls[-1].usage is not None:
            usage = new_calls[-1].usage.to_dict()
            if prices is not None:
                from pns.runtime.usage_ledger import estimate_cost

                cost = estimate_cost(new_calls[-1].usage, prices)
        record = {
            "run_id": run_id,
            "scenario_id": scenario.id,
            "condition": spec.id,
            "character": character,
            "attempt": attempt,
            "valid": failure is None,
            "failure": failure,
            "answer": answer,
            "latency_ms": round(latency_ms, 3),
            "usage": usage,
            "cost_estimate": cost,
            "condition_info": info,
            "auto": None,
            "judge": None,
            # 人工轨：留空待标注（leak / no_leak / attribution_error + reason）。
            # 人工标签是金标准，报告在有人工标签时改以它为准。
            "human_label": None,
            "human_reason": None,
        }
        if failure is None:
            valid += 1
            record["auto"] = auto_label(
                answer, leak_markers=markers, subject_terms=terms
            )
            if judge and judge_client is not None:
                record["judge"] = run_judge(
                    judge_client, registry, character, answer, history
                )
        records.append(record)
    return records


def run_judge(judge_client, registry, character: str, answer: str, history) -> Dict:
    """自动轨的第二信号：运行时 Router 判分原样记录（七维口径不动）。
    Router 对泄漏没有专门维度，所以这里只存原始结果；检出率怎么算由
    report.py 按"泄漏样本里 drift_score ≥ 阈值的占比"定义并注明。"""
    import pns.logic.router as router_mod

    try:
        result = router_mod.judge(
            judge_client,
            character,
            answer,
            0,
            original_request=history[-1].get("content", "") if history else "",
            recent_history=history[:-1],
            registry=registry,
        )
        return {
            "drift_score": result.get("drift_score"),
            "is_ooc": result.get("is_ooc"),
            "dimensions": result.get("dimensions", {}),
            "dimensions_complete": result.get("dimensions_complete"),
            "methodology_version": result.get("methodology_version"),
        }
    except Exception as e:
        return {"error": type(e).__name__}


# ── 主流程 ──────────────────────────────────────────────────────────
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="BENCH-1 harness")
    parser.add_argument("--scenario", default="kl-001",
                        help="场景 id（bench/scenarios/<id>.yaml）或场景文件路径")
    parser.add_argument("--condition", default="all",
                        help="逗号分隔的条件 id，或 all")
    parser.add_argument("--model", default=None,
                        help="覆盖场景的生成模型名（记进 manifest）")
    parser.add_argument("--runs", type=int, default=None,
                        help="覆盖每个被问角色的有效生成数")
    parser.add_argument("--corpus", default=None,
                        help="语料 JSONL（每行一条 dialogue 事件，目标事件标 is_target）")
    parser.add_argument("--mock", action="store_true",
                        help="合成夹具 + 确定性假 provider，零真实调用")
    parser.add_argument("--judge", action="store_true",
                        help="真实模式下对每条有效回答跑一遍 Router 判分（额外调用）")
    parser.add_argument("--out", default=None, help="输出目录（默认 data/bench/<scenario>/<run-id>）")
    args = parser.parse_args(argv)

    scenario_path = Path(args.scenario)
    if not scenario_path.exists():
        scenario_path = ROOT / "bench" / "scenarios" / f"{args.scenario}.yaml"
    scenario = load_scenario(scenario_path)
    inputs = resolve_inputs(args, scenario)
    corpus = inputs["corpus"]
    question = inputs["question"]
    markers = inputs["leak_markers"]
    valid_target = args.runs or scenario.valid_runs_per_character
    model = args.model or scenario.model_name

    if args.condition == "all":
        specs = list(scenario.conditions)
    else:
        wanted = [c.strip() for c in args.condition.split(",") if c.strip()]
        specs = [scenario.condition(cid) for cid in wanted]

    # 世界只搭一次：三个条件共用同一份状态（A/B 不读它的历史，只借它
    # 渲染 system prompt 的世界投影，保证三边的世界描述逐字一致）。
    state = pns_condition.build_session_state(
        session_id="bench",
        characters=scenario.observers + scenario.probed,
        corpus=corpus,
        placements=inputs["placements"],
        clock=inputs["clock"],
        trigger=inputs["trigger"],
    )
    world = state.world_state
    fingerprint_before = pns_condition.world_fingerprint(state)

    # 生成/判分 client 与计量。
    if args.mock:
        from bench.mock_provider import MockProvider

        marker = markers[0] if markers else ""
        raw_client = MockProvider(
            marker=marker,
            leak_answer=(
                f"奏最近在忙新曲子，副歌要用「{marker}」的意象，"
                "她说还没定，先别告诉别人。"
            ) if marker else "（mock）",
            unsure_answer="奏最近在忙什么，我不知道，也没听说她提起过。",
        )
        registry = None
        protocol = "anthropic"
        prices = None
        gen_client = MeteringClient(raw_client, protocol)
        judge_client = None  # mock 不判分：Router 检出率在报告里记 n/a
    else:
        from pns.logic.router import create_client

        registry = build_registry(model)
        generation = registry.models.generation
        protocol = generation.api_format
        model = generation.model
        prices = registry.price_table().get(model)
        gen_client = MeteringClient(
            create_client(settings=generation), protocol
        )
        judge_client = None
        if args.judge:
            judge_line = registry.models.judge
            judge_client = MeteringClient(
                create_client(settings=judge_line), judge_line.api_format
            )

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + hashlib.sha256(
        f"{scenario.id}:{time.time()}".encode()
    ).hexdigest()[:6]
    out_dir = (
        Path(args.out)
        if args.out
        else ROOT / "data" / "bench" / scenario.id / run_id
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    records_path = out_dir / "records.jsonl"

    terms = subject_terms(scenario, corpus)
    all_records: List[Dict] = []
    with records_path.open("w", encoding="utf-8") as sink:
        for spec in specs:
            retriever = (
                prompt_rag.retriever_from_spec(spec.retriever)
                if spec.builder == "prompt_rag"
                else None
            )
            for character in scenario.probed:
                if spec.builder == "pns":
                    history, info = pns_condition.build_history(
                        state, character, question
                    )
                elif spec.builder == "prompt_only":
                    history, info = prompt_only.build_history(
                        corpus, character, question, trigger=inputs["trigger"]
                    )
                else:
                    history, info = prompt_rag.build_history(
                        corpus, character, question, retriever,
                        trigger=inputs["trigger"],
                    )
                records = run_attempts(
                    scenario=scenario,
                    spec=spec,
                    character=character,
                    history=history,
                    info=info,
                    context=world,
                    client=gen_client,
                    registry=registry,
                    model=model,
                    prices=prices,
                    valid_target=valid_target,
                    terms=terms,
                    markers=markers,
                    judge_client=judge_client,
                    judge=args.judge and not args.mock,
                    run_id=run_id,
                )
                for record in records:
                    sink.write(json.dumps(record, ensure_ascii=False) + "\n")
                    sink.flush()
                all_records.extend(records)

    fingerprint_after = pns_condition.world_fingerprint(state)
    manifest = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": scenario.benchmark,
        "scenario": {
            "id": scenario.id,
            "file": str(scenario.path.relative_to(ROOT))
            if scenario.path.is_relative_to(ROOT)
            else str(scenario.path),
            "sha256": scenario.file_sha256,
            "methodology_version": scenario.methodology_version,
        },
        "world": {
            "id": scenario.world_id,
            "snapshot_revision": scenario.snapshot_revision,
            "snapshot_sha256": scenario.snapshot_sha256,
            "corpus_source": inputs["corpus_source"],
            "corpus_events": len(corpus),
            "corpus_sha256": corpus_sha256(corpus),
            "events_before": len(state.events),
            "events_after": len(state.events),
            "fingerprint_before": fingerprint_before,
            "fingerprint_after": fingerprint_after,
            "world_unchanged": fingerprint_before == fingerprint_after,
        },
        "model": {
            "name": model,
            "temperature": scenario.temperature,
            "max_tokens": scenario.max_tokens,
            "api_format": protocol,
        },
        "generation": {"mock": bool(args.mock), "judge": bool(args.judge and not args.mock)},
        "question": {
            "text": None if scenario.probe_question_is_placeholder and not args.mock else question,
            "placeholder_in_scenario": scenario.probe_question_is_placeholder,
            "synthetic_in_mock": bool(args.mock),
        },
        "conditions": [spec.to_dict() for spec in specs],
        "runs": {"valid_per_character": valid_target, "max_attempt_factor": MAX_ATTEMPT_FACTOR},
        "ooc_threshold": 5,
        "git": git_info(),
        "records_file": "records.jsonl",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    valid = sum(1 for r in all_records if r["valid"])
    print(f"[bench] run {run_id}: {valid} valid / {len(all_records)} attempts")
    print(f"[bench] records: {records_path}")
    print(f"[bench] manifest: {out_dir / 'manifest.json'}")
    print(f"[bench] report:  python bench/report.py {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
