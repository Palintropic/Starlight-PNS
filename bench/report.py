# bench/report.py — 把一次运行的 records + manifest 汇成 KL-001 同构报告。
#
#   python bench/report.py data/bench/kl-001/<run-id> [--out report.md]
#
# 指标口径（Issue #43 的 Metrics 一节，逐条对应）：
#   knowledge leakage rate   泄漏数 / 有效数；标签金标准 = 人工，其次自动轨
#   unsupported claim rate   自动轨 unsupported_claim / 有效数（候选口径，注明）
#   correct uncertainty rate 自动轨 correct_uncertainty / 有效数
#   Router detection rate    已判分的泄漏样本里 drift_score ≥ 阈值的占比；
#                            没跑判分就是 n/a，不许按 0 报
#   model calls / token usage / latency / cost
#                            全部尝试（含失败）计数；成本缺 usage 时 n/a
# 聚合是纯函数（aggregate），渲染只读聚合结果——数字怎么来的必须可测。
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.labels import gold_leak


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return (numerator / denominator) if denominator else None


def aggregate(records: List[Dict], *, ooc_threshold: float = 5.0) -> Dict:
    """records → {(condition, character): cell, (condition,): total} 形状
    的聚合字典（键 "by_cell" / "by_condition"）。"""
    cells: Dict = {}
    for record in records:
        key = (record["condition"], record["character"])
        cell = cells.setdefault(
            key,
            {
                "condition": record["condition"],
                "character": record["character"],
                "attempts": 0,
                "valid": 0,
                "failures": {},
                "leaks": 0,
                "leak_basis_human": 0,
                "leak_evaluable": 0,
                "attribution_errors": 0,
                "unsupported_claims": 0,
                "correct_uncertainty": 0,
                "judged": 0,
                "judge_detected": 0,
                "judge_on_leaks": 0,
                "latency_ms_total": 0.0,
                "tokens": {"fresh_input": 0, "cache_write": 0, "cache_read": 0, "output": 0},
                "cost": 0.0,
                "cost_known": True,
                "retrieval_runs": 0,
                "retrieval_target_hits": 0,
                "retrieval_target_ranks": [],
            },
        )
        cell["attempts"] += 1
        if not record["valid"]:
            kind = record.get("failure") or "unknown"
            cell["failures"][kind] = cell["failures"].get(kind, 0) + 1
            continue
        cell["valid"] += 1
        cell["latency_ms_total"] += record.get("latency_ms") or 0.0
        usage = record.get("usage")
        if usage:
            for part in cell["tokens"]:
                cell["tokens"][part] += usage.get(part, 0)
        if record.get("cost_estimate") is None:
            cell["cost_known"] = False
        else:
            cell["cost"] += record["cost_estimate"]

        leak = gold_leak(record)
        if leak is not None:
            cell["leak_evaluable"] += 1
            if record.get("human_label") in ("leak", "no_leak", "attribution_error"):
                cell["leak_basis_human"] += 1
            if leak:
                cell["leaks"] += 1
        if record.get("human_label") == "attribution_error":
            cell["attribution_errors"] += 1
        auto = record.get("auto") or {}
        if auto.get("unsupported_claim"):
            cell["unsupported_claims"] += 1
        if auto.get("correct_uncertainty"):
            cell["correct_uncertainty"] += 1

        judge = record.get("judge")
        if judge and judge.get("drift_score") is not None:
            cell["judged"] += 1
            if leak:
                cell["judge_on_leaks"] += 1
                if judge["drift_score"] >= ooc_threshold:
                    cell["judge_detected"] += 1

        retrieved = (record.get("condition_info") or {}).get("retrieved")
        if retrieved is not None:
            cell["retrieval_runs"] += 1
            target_hits = [h for h in retrieved if h.get("is_target")]
            if target_hits:
                cell["retrieval_target_hits"] += 1
                cell["retrieval_target_ranks"].append(
                    min(h["rank"] for h in target_hits)
                )

    def finalize(cell: Dict) -> Dict:
        out = dict(cell)
        out["leakage_rate"] = _rate(cell["leaks"], cell["leak_evaluable"])
        out["unsupported_claim_rate"] = _rate(cell["unsupported_claims"], cell["valid"])
        out["correct_uncertainty_rate"] = _rate(cell["correct_uncertainty"], cell["valid"])
        out["router_detection_rate"] = _rate(cell["judge_detected"], cell["judge_on_leaks"])
        out["mean_latency_ms"] = (
            cell["latency_ms_total"] / cell["valid"] if cell["valid"] else None
        )
        out["cost_total"] = cell["cost"] if cell["cost_known"] else None
        out["retrieval_target_hit_rate"] = _rate(
            cell["retrieval_target_hits"], cell["retrieval_runs"]
        )
        return out

    by_cell = {key: finalize(cell) for key, cell in cells.items()}
    by_condition: Dict[str, Dict] = {}
    for (condition, _character), cell in by_cell.items():
        total = by_condition.setdefault(
            condition,
            {
                "condition": condition, "character": "*",
                "attempts": 0, "valid": 0, "failures": {},
                "leaks": 0, "leak_basis_human": 0, "leak_evaluable": 0,
                "attribution_errors": 0, "unsupported_claims": 0,
                "correct_uncertainty": 0, "judged": 0, "judge_detected": 0,
                "judge_on_leaks": 0, "latency_ms_total": 0.0,
                "tokens": {"fresh_input": 0, "cache_write": 0, "cache_read": 0, "output": 0},
                "cost": 0.0, "cost_known": True,
                "retrieval_runs": 0, "retrieval_target_hits": 0,
                "retrieval_target_ranks": [],
            },
        )
        for key in (
            "attempts", "valid", "leaks", "leak_basis_human", "leak_evaluable",
            "attribution_errors", "unsupported_claims", "correct_uncertainty",
            "judged", "judge_detected", "judge_on_leaks", "retrieval_runs",
            "retrieval_target_hits",
        ):
            total[key] += cell[key]
        total["latency_ms_total"] += cell["latency_ms_total"]
        for part in total["tokens"]:
            total["tokens"][part] += cell["tokens"][part]
        total["cost"] += cell["cost"]
        total["cost_known"] = total["cost_known"] and cell["cost_known"]
        for kind, count in cell["failures"].items():
            total["failures"][kind] = total["failures"].get(kind, 0) + count
        total["retrieval_target_ranks"].extend(cell["retrieval_target_ranks"])
    by_condition = {key: finalize(cell) for key, cell in by_condition.items()}
    return {"by_cell": by_cell, "by_condition": by_condition}


# ── 渲染 ────────────────────────────────────────────────────────────
def _pct(rate: Optional[float]) -> str:
    return "n/a" if rate is None else f"{rate * 100:.0f}%"


def _count_rate(numerator: int, denominator: int) -> str:
    return f"{numerator}/{denominator} ({_pct(_rate(numerator, denominator))})"


def render_markdown(manifest: Dict, agg: Dict, records: List[Dict]) -> str:
    scenario = manifest["scenario"]
    world = manifest["world"]
    model = manifest["model"]
    lines = []
    lines.append(f"# {manifest['benchmark']} / {scenario['id'].upper()} — Harness Run")
    lines.append("")
    lines.append(f"**Run:** `{manifest['run_id']}`")
    lines.append("")
    lines.append(f"**Model:** `{model['name']}`")
    lines.append("")
    lines.append(f"**World:** `{world['id']}`")
    lines.append("")
    if world.get("snapshot_revision") is not None:
        lines.append(f"**Fixed snapshot:** revision `{world['snapshot_revision']}`")
        lines.append("")
    if world.get("snapshot_sha256"):
        lines.append(f"**Snapshot SHA-256:** `{world['snapshot_sha256']}`")
        lines.append("")
    lines.append(f"**Temperature:** `{model['temperature']}`")
    lines.append("")
    lines.append(f"**Max tokens:** `{model['max_tokens']}`")
    lines.append("")
    if manifest["generation"].get("mock"):
        lines.append(
            "> ⚠️ **MOCK RUN** — 合成夹具 + 确定性假 provider。数字只证明管线"
            "可运行，不构成任何基线结果。"
        )
        lines.append("")

    characters = []
    for (condition, character) in agg["by_cell"]:
        if character not in characters:
            characters.append(character)
    conditions = list(agg["by_condition"].keys())

    lines.append("### Results")
    lines.append("")
    header = "| Condition | " + " | ".join(c.capitalize() for c in characters) + " | Total target leakage |"
    lines.append(header)
    lines.append("|" + "---|" * (len(characters) + 2))
    for condition in conditions:
        row = [condition]
        for character in characters:
            cell = agg["by_cell"].get((condition, character))
            row.append(
                _count_rate(cell["leaks"], cell["leak_evaluable"]) if cell else "—"
            )
        total = agg["by_condition"][condition]
        row.append(_count_rate(total["leaks"], total["leak_evaluable"]))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    total_valid = sum(c["valid"] for c in agg["by_condition"].values())
    lines.append(f"These are {total_valid} evaluable generations in total.")
    lines.append("")

    lines.append("### Metrics by condition")
    lines.append("")
    lines.append("| Condition | Unsupported claim rate | Correct uncertainty rate | Router detection rate | Model calls | Tokens (in/out) | Mean latency | Cost |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for condition in conditions:
        total = agg["by_condition"][condition]
        tokens = total["tokens"]
        in_total = tokens["fresh_input"] + tokens["cache_write"] + tokens["cache_read"]
        cost = "n/a" if total["cost_total"] is None else f"{total['cost_total']:.4f}"
        latency = (
            "n/a" if total["mean_latency_ms"] is None
            else f"{total['mean_latency_ms']:.0f} ms"
        )
        lines.append(
            f"| {condition} | {_pct(total['unsupported_claim_rate'])} "
            f"| {_pct(total['correct_uncertainty_rate'])} "
            f"| {_pct(total['router_detection_rate'])} "
            f"| {total['attempts']} | {in_total}/{tokens['output']} "
            f"| {latency} | {cost} |"
        )
    lines.append("")

    rag = agg["by_condition"].get("prompt_rag")
    if rag and rag["retrieval_runs"]:
        lines.append("### Shared-memory retrieval condition")
        lines.append("")
        ranks = rag["retrieval_target_ranks"]
        rank_note = (
            f"，命中时目标事件的最好名次分布：{sorted(ranks)}" if ranks else ""
        )
        lines.append(
            f"目标事件在 {rag['retrieval_target_hits']}/{rag['retrieval_runs']} 次"
            f"有效生成中被检索到{rank_note}。"
        )
        lines.append("")

    attribution = sum(
        c["attribution_errors"] for c in agg["by_condition"].values()
    )
    if attribution:
        lines.append("### Additional observation")
        lines.append("")
        lines.append(
            f"人工标注中共 {attribution} 条归属错误（单列统计，不计入泄漏数）。"
        )
        lines.append("")

    lines.append("### Failure accounting")
    lines.append("")
    any_failure = False
    for condition in conditions:
        failures = agg["by_condition"][condition]["failures"]
        for kind, count in sorted(failures.items()):
            any_failure = True
            lines.append(f"- {condition}: {kind} × {count}（已排除并补跑）")
    if not any_failure:
        lines.append("- 无失败尝试。")
    lines.append("")

    labeled = sum(
        1 for r in records if r.get("human_label") in ("leak", "no_leak", "attribution_error")
    )
    valid_records = sum(1 for r in records if r["valid"])
    lines.append("### Evaluation status")
    lines.append("")
    if labeled:
        lines.append(
            f"人工标签覆盖 {labeled}/{valid_records} 条有效生成；有人工标签处"
            "泄漏率以人工为准，其余以自动轨（标记命中）为准。"
        )
    else:
        lines.append(
            f"人工标签覆盖 0/{valid_records} 条：当前泄漏率全部来自自动轨"
            "（标记命中启发式），**不是**金标准结论，待人工标注后重算。"
        )
    lines.append(
        "Router 判分只是第二信号（自动轨），不作为 PNS 更优的唯一证据"
        "——与 Issue #43 的评估要求一致。"
    )
    lines.append("")

    lines.append("### Reproducibility")
    lines.append("")
    lines.append(f"- scenario file: `{scenario['file']}` (sha256 `{scenario['sha256']}`)")
    lines.append(f"- corpus: {world['corpus_source']}, {world['corpus_events']} events (sha256 `{world['corpus_sha256']}`)")
    lines.append(f"- methodology: `{scenario['methodology_version']}`")
    lines.append(f"- git: `{manifest['git'].get('commit')}` (branch `{manifest['git'].get('branch')}`, dirty={manifest['git'].get('dirty')})")
    lines.append(
        f"- world unchanged by probing: {world.get('world_unchanged')} "
        f"(events {world.get('events_before')} → {world.get('events_after')})"
    )
    lines.append(f"- mock: {manifest['generation'].get('mock')}")
    lines.append("")
    lines.append("Results are retained whether they support or reject the hypothesis.")
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="BENCH-1 report")
    parser.add_argument("run_dir", help="run_bench.py 的输出目录")
    parser.add_argument("--out", default=None, help="报告输出路径（默认 <run_dir>/report.md）")
    args = parser.parse_args(argv)
    run_dir = Path(args.run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in (run_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    agg = aggregate(records, ooc_threshold=manifest.get("ooc_threshold", 5))
    text = render_markdown(manifest, agg, records)
    out = Path(args.out) if args.out else run_dir / "report.md"
    out.write_text(text, encoding="utf-8")
    print(f"[bench] report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
