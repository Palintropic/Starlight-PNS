"""MODEL-1 model comparison: re-judge archived lines and sample lines per model.

Read-only against world archives. Point it at a *copy* of a checkpoint; it
never opens a world through the lifecycle service, never takes OWNER.lock and
never writes under --world-root.

    # judge scale: re-judge every archived audit with drift_score >= 3
    python scripts/model1_compare.py judge --world-root /tmp/w --world yoake-mae \
        --runs mimo-v2.5-pro:2,mimo-v2.6-pro:1 --budget-yuan 2 --out judge.jsonl

    # taste: sample each resident's next line at the archived clock, per model
    python scripts/model1_compare.py taste --world-root /tmp/w --world yoake-mae \
        --models mimo-v2.5-pro,mimo-v2.6-pro --samples 3 --budget-yuan 2 \
        --out taste.jsonl --sheet taste_blind.md

    # offline summary of a judge run
    python scripts/model1_compare.py summary judge.jsonl

Every provider call is recorded with its usage and an estimated cost
(MiMo pay-as-you-go: input 3, cache hit 0.025, output 6 yuan per million
tokens). --budget-yuan stops the run before the next call once the estimate
reaches the cap.
"""
import argparse
import glob
import json
import random
import sys
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PRICE_PER_M = {"input": 3.0, "cache_read": 0.025, "output": 6.0}
THRESHOLD = 5.0
JUDGE_FAILURES = {"error", "解析失败"}


class BudgetExceeded(RuntimeError):
    pass


def estimate_yuan(usage):
    if not usage:
        return 0.0
    # Anthropic-format usage: input_tokens excludes cache reads and cache
    # writes, which are reported separately (MiMo bills writes as input).
    fresh = (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
    cached = usage.get("cache_read_input_tokens") or 0
    out = usage.get("output_tokens") or 0
    return (
        fresh * PRICE_PER_M["input"]
        + cached * PRICE_PER_M["cache_read"]
        + out * PRICE_PER_M["output"]
    ) / 1_000_000


class Meter:
    """Wraps an Anthropic-format client; records usage of every messages.create."""

    def __init__(self, client, budget_yuan):
        self._client = client
        self.budget_yuan = budget_yuan
        self.spent_yuan = 0.0
        self.calls = 0
        self.last_usage = None
        self.last_stop_reason = None
        self.last_block_types = None
        # router.judge and the generator turn every exception into a result,
        # so callers must check this flag rather than rely on the raise.
        self.exhausted = False
        self.messages = self

    def create(self, **request):
        if self.spent_yuan >= self.budget_yuan:
            self.exhausted = True
            raise BudgetExceeded(
                f"estimated spend {self.spent_yuan:.4f} reached --budget-yuan {self.budget_yuan}"
            )
        self.last_usage = None
        response = self._client.messages.create(**request)
        usage = getattr(response, "usage", None)
        if usage is not None:
            dump = getattr(usage, "model_dump", None)
            usage = dump() if callable(dump) else dict(vars(usage))
        self.last_usage = usage
        self.last_stop_reason = getattr(response, "stop_reason", None)
        self.last_block_types = [
            getattr(block, "type", type(block).__name__)
            for block in getattr(response, "content", [])
        ]
        self.calls += 1
        self.spent_yuan += estimate_yuan(usage)
        return response


def load_registry(*, generator_model=None, evaluator_model=None):
    from dotenv import load_dotenv
    from pns.runtime.content_registry import ENV_PATH, build_content_registry

    load_dotenv(ENV_PATH, override=True)
    registry = build_content_registry()
    models = registry.models
    if generator_model:
        models = replace(models, generator_model=generator_model)
    if evaluator_model:
        models = replace(models, evaluator_model=evaluator_model)
    if models.api_format == "openai":
        raise SystemExit("this script only meters the anthropic-format path used in production")
    return replace(registry, models=models)


def make_meter(registry, budget_yuan):
    from pns.logic import router as router_mod

    if not registry.models.api_key:
        raise SystemExit("missing API key")
    client = router_mod.create_client(registry.models.api_key, settings=registry.models)
    return Meter(client, budget_yuan)


def write_record(fh, record):
    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    fh.flush()


def guard_output(paths, world_root):
    root = Path(world_root).resolve()
    for path in paths:
        if path is not None and Path(path).resolve().is_relative_to(root):
            raise SystemExit(f"{path} must not be inside --world-root")


def parse_runs(text):
    runs = []
    for item in text.split(","):
        model, _, reps = item.strip().partition(":")
        runs.append((model, int(reps or 1)))
    if not runs or any(not m or r < 1 for m, r in runs):
        raise SystemExit("--runs looks like mimo-v2.5-pro:2,mimo-v2.6-pro:1")
    return runs


# ── judge ──────────────────────────────────────────────────────────────
def read_raw_world(world_dir):
    world = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
    events = []
    for path in sorted(glob.glob(str(world_dir / "history" / "events-*.jsonl"))):
        with open(path, encoding="utf-8") as fh:
            events.extend(json.loads(line) for line in fh if line.strip())
    events.extend(world["state"]["events"]["events"])
    events.sort(key=lambda e: e["sequence"])
    return world, events


def audited_records(world, min_score):
    records = []
    for record in world["state"]["agency"]["log"]["records"]:
        detail = record.get("detail")
        audit = detail.get("audit") if isinstance(detail, dict) else None
        if isinstance(audit, dict) and audit.get("drift_score", -1) >= min_score:
            records.append(record)
    return records


class SituationReplay:
    """Each resident's location / activity / channels at a past instant.

    The archive keeps only the final WorldState, so this replays the
    state-changing events in sequence. Values before a resident's first event
    of a kind are inferred: channel membership from whether the first presence
    event is a leave, location from where they spoke before their first move.
    Anything still unknown renders as "unknown", the same word production uses.
    """

    def __init__(self, events, residents):
        self.events = events
        self.residents = residents
        self.initial_location = {}
        self.initial_channels = {}
        for cid in residents:
            first_move = next(
                (e for e in events
                 if e["type"] == "character.location_changed" and e["actor_id"] == cid),
                None,
            )
            spoke = next(
                (e for e in events
                 if e["type"] == "dialogue.spoken" and e["actor_id"] == cid
                 and (first_move is None or e["sequence"] < first_move["sequence"])),
                None,
            )
            self.initial_location[cid] = spoke["location_id"] if spoke else None
            channels = set()
            seen = set()
            for e in events:
                if e["actor_id"] != cid or e["type"] not in (
                    "presence.joined_channel", "presence.left_channel"
                ):
                    continue
                if e["channel_id"] in seen:
                    continue
                seen.add(e["channel_id"])
                if e["type"] == "presence.left_channel":
                    channels.add(e["channel_id"])
            self.initial_channels[cid] = channels

    def at(self, before_sequence):
        location = dict(self.initial_location)
        activity = {cid: None for cid in self.residents}
        channels = {cid: set(v) for cid, v in self.initial_channels.items()}
        for e in self.events:
            if e["sequence"] >= before_sequence:
                break
            cid = e["actor_id"]
            if cid not in location:
                continue
            if e["type"] == "character.location_changed":
                location[cid] = e["location_id"]
            elif e["type"] == "character.activity_changed":
                activity[cid] = e["payload"].get("activity")
            elif e["type"] == "presence.joined_channel":
                channels[cid].add(e["channel_id"])
            elif e["type"] == "presence.left_channel":
                channels[cid].discard(e["channel_id"])
        return location, activity, channels


def situation_facts(actor, when, location, activity, channels):
    # Same wording as AutonomyCoordinator._audit_request.
    here = location.get(actor)
    own_channels = tuple(sorted(channels.get(actor, ())))
    co_located = tuple(
        cid for cid in sorted(location)
        if cid != actor and here is not None and location[cid] == here
    )
    peers = tuple(sorted({
        cid for cid, joined in channels.items()
        if cid != actor and cid not in co_located and joined & set(own_channels)
    }))
    return (
        f"模拟时间：{when.isoformat(timespec='minutes')}",
        f"自己的 location_id：{here or 'unknown'}",
        f"自己的当前活动：{activity.get(actor) or 'unknown'}",
        f"自己加入的 channel_id：{', '.join(own_channels) if own_channels else 'none'}",
        f"与自己同处一地的角色 ID：{', '.join(co_located) if co_located else 'none'}",
        f"仅与自己同在线频道、并非同处一地的角色 ID："
        f"{', '.join(peers) if peers else 'none'}",
    )


def recent_lines_for(observations, actor, decided_at, own_event_id, limit):
    from pns.models.observation import Observation

    lines = []
    for raw in observations:
        if raw["observer_id"] != actor or raw["source_event_id"] == own_event_id:
            continue
        if raw["observed_at"] > decided_at:
            continue
        line = Observation.from_dict(raw).render_line()
        if line is not None:
            lines.append(line)
    return lines[-limit:]


def build_judge_items(world_dir, min_score):
    from pns.runtime.autonomy.coordinator import _AUDIT_RECENT_LINES

    world, events = read_raw_world(world_dir)
    residents = list(world["state"]["characters"])
    replay = SituationReplay(events, residents)
    seq_of = {e["event_id"]: e["sequence"] for e in events}
    observations = world["state"]["observations"]["observations"]
    items = []
    for record in audited_records(world, min_score):
        audit = record["detail"]["audit"]
        actor = record["character_id"]
        decided_at = record["decided_at"]
        event_id = record.get("event_id")
        # Accepted lines have an event; rejected ones do not. Either way the
        # situation is the one in force before anything at this minute that
        # followed it, so cut at the line's own event or at the first event
        # after the decision time.
        if event_id in seq_of:
            cut = seq_of[event_id]
        else:
            cut = next(
                (e["sequence"] for e in events if e["occurred_at"] > decided_at),
                events[-1]["sequence"] + 1,
            )
        location, activity, channels = replay.at(cut)
        items.append({
            "key": record["due_id"] + "#" + str(record.get("sequence", 0)),
            "character_id": actor,
            "decided_at": decided_at,
            "outcome": record["outcome"],
            "text": audit["payload"]["text"],
            "original": {
                "drift_score": audit["drift_score"],
                "accepted": audit["accepted"],
                "evaluator_model": audit.get("evaluator_model"),
                "methodology_version": audit.get("methodology_version"),
            },
            "recent_lines": recent_lines_for(
                observations, actor, decided_at, event_id, _AUDIT_RECENT_LINES
            ),
            "situation_facts": list(situation_facts(
                actor, datetime.fromisoformat(decided_at), location, activity, channels
            )),
        })
    return items


def cmd_judge(args):
    from pns.logic import router as router_mod
    from pns.runtime.autonomy.context import DIALOGUE_OUTPUT_RULES

    guard_output([args.out], args.world_root)
    items = build_judge_items(Path(args.world_root) / args.world, args.min_score)
    if args.limit:
        items = items[: args.limit]
    runs = parse_runs(args.runs)
    print(f"{len(items)} lines x {sum(r for _, r in runs)} judgements", file=sys.stderr)
    if args.dry_run:
        print(json.dumps(items[:3], ensure_ascii=False, indent=2))
        return 0
    registries = {m: load_registry(evaluator_model=m) for m, _ in runs}
    meter = make_meter(next(iter(registries.values())), args.budget_yuan)
    rng = random.Random(args.seed)
    with open(args.out, "a", encoding="utf-8") as fh:
        try:
            for item in items:
                schedule = [(m, rep) for m, reps in runs for rep in range(reps)]
                # Interleave models per line so provider drift over the run
                # does not land on one model only.
                rng.shuffle(schedule)
                for model, rep in schedule:
                    started = time.monotonic()
                    result = router_mod.judge(
                        meter, item["character_id"], item["text"], 0,
                        recent_history=[
                            {"role": "assistant", "content": line}
                            for line in item["recent_lines"]
                        ],
                        original_request="\n".join(DIALOGUE_OUTPUT_RULES),
                        situation_facts=item["situation_facts"],
                        registry=registries[model],
                    )
                    if meter.exhausted:
                        raise BudgetExceeded("budget reached")
                    failed = result.get("drift_type") in JUDGE_FAILURES
                    write_record(fh, {
                        "kind": "judge",
                        "key": item["key"],
                        "character_id": item["character_id"],
                        "text": item["text"],
                        "original": item["original"],
                        "model": result.get("evaluator_model"),
                        "rep": rep,
                        "failed": failed,
                        "drift_score": None if failed else result["drift_score"],
                        "dimensions": result.get("dimensions"),
                        "dimensions_complete": result.get("dimensions_complete"),
                        "needs_human_review": result.get("needs_human_review"),
                        "drift_type": result.get("drift_type"),
                        "reason": result.get("reason"),
                        "usage": meter.last_usage,
                        "stop_reason": meter.last_stop_reason,
                        "block_types": meter.last_block_types,
                        "seconds": round(time.monotonic() - started, 2),
                    })
                    print(
                        f"[{meter.calls}] {model} {item['key']} "
                        f"{'FAILED' if failed else result['drift_score']} "
                        f"(orig {item['original']['drift_score']}) "
                        f"spent~{meter.spent_yuan:.3f}",
                        file=sys.stderr,
                    )
        except BudgetExceeded as e:
            print(f"stopped: {e}", file=sys.stderr)
    print(f"calls={meter.calls} estimated_yuan={meter.spent_yuan:.4f}", file=sys.stderr)
    return 0


# ── taste ──────────────────────────────────────────────────────────────
def taste_contexts(store, world_id, registry, residents):
    """One GenerationContext per (resident, legal speech action) at the archived clock."""
    from pns.models.action import ActionId
    from pns.models.activation import ActivationDue, ActivationKind
    from pns.models.agency import AgencyBudget
    from pns.runtime.agency.context import build_agency_context
    from pns.runtime.autonomy.context import build_generation_context
    from pns.runtime.memory.projection import recalled_lines
    from pns.runtime.memory.recall import MemoryRecall
    from pns.runtime.persistence import validate_world_id

    world_id = validate_world_id(world_id)
    path = store.archive_path(world_id)
    before = path.read_bytes()
    archive = store.load(world_id)
    if path.read_bytes() != before:
        raise SystemExit("archive changed while reading; use a still checkpoint copy")
    state = archive.restore_state()
    world = state.world_state
    now = world.clock
    budget = AgencyBudget()
    speech = {ActionId.SPEAK_HERE, ActionId.SEND_CHANNEL_MESSAGE}
    contexts = []
    for cid in residents or world.known_characters():
        due = ActivationDue(
            activation_id=f"model1-taste-{cid}",
            kind=ActivationKind.CHARACTER_ACTIVATION,
            due_at=now, fired_at=now, sequence=0, character_id=cid,
        )
        agency = build_agency_context(
            world, cid, due, state.observations.for_character(cid),
            max_legal_actions=budget.max_legal_actions,
            max_observations=budget.max_observations,
        )
        recalled = MemoryRecall(state).recall_for(cid)
        visible = {o.source_event_id for o in agency.observations}
        lines = recalled_lines(recalled, exclude_source_event_ids=visible)
        for choice in agency.legal_actions:
            if choice.action_id not in speech:
                continue
            context = build_generation_context(
                agency, choice, lines, recall_truncated=recalled.truncated
            )
            contexts.append({
                "context_id": f"{cid}:{choice.action_id.value}:{choice.target_id or '-'}",
                "character_id": cid,
                "action_id": choice.action_id.value,
                "target_id": choice.target_id,
                "location_id": agency.location_id,
                "activity": agency.activity,
                "co_located": list(agency.co_located_characters),
                "channel_peers": list(agency.channel_characters),
                "observations": len(agency.observations),
                "recalled": len(lines),
                "context": context,
            })
    return archive, now, contexts


def cmd_taste(args):
    from pns.interfaces.composition import AutonomySettings
    from pns.logic.simulation import GenerationTruncated, call_character
    from pns.runtime.autonomy.generation import ABSTAIN_TOKEN, GenerationError, parse_line
    from pns.runtime.autonomy.prompt import PromptedLineGenerator
    from pns.runtime.persistence import FileWorldStore

    guard_output([args.out, args.sheet], args.world_root)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if len(models) != 2:
        raise SystemExit("--models takes exactly two models for a blind A/B sheet")
    residents = [c.strip() for c in args.residents.split(",")] if args.residents else None
    base = load_registry()
    archive, now, contexts = taste_contexts(
        FileWorldStore(args.world_root), args.world, base, residents
    )
    print(f"{len(contexts)} contexts at {now.isoformat()} x {len(models)} models "
          f"x {args.samples} samples", file=sys.stderr)
    if args.dry_run:
        for c in contexts:
            print(json.dumps({k: v for k, v in c.items() if k != "context"}, ensure_ascii=False))
        return 0
    autonomy = AutonomySettings.from_env()
    meter = make_meter(base, args.budget_yuan)
    names = {cid: base.character_name(cid) for cid in sorted(base.characters)}
    generators = {}
    for model in models:
        registry = load_registry(generator_model=model)

        def call_model(cid, view, history, registry=registry):
            try:
                return call_character(
                    meter, cid, history, view, registry.models.generator_model,
                    autonomy.max_tokens, autonomy.temperature, registry=registry,
                )
            except GenerationTruncated as e:
                raise GenerationError("truncated at max_tokens", retryable=True) from e

        generators[model] = PromptedLineGenerator(
            call_model, locations=registry.new_location_graph(),
            channels=registry.new_channel_registry(), names=names,
        )
    rng = random.Random(args.seed)
    samples = {}
    with open(args.out, "a", encoding="utf-8") as fh:
        try:
            for item in contexts:
                schedule = [(m, i) for m in models for i in range(args.samples)]
                rng.shuffle(schedule)
                for model, i in schedule:
                    started = time.monotonic()
                    line, error = None, None
                    try:
                        raw = generators[model].generate(item["context"])
                        line = parse_line(raw, item["context"])
                    except GenerationError as e:
                        error = str(e)
                    if meter.exhausted:
                        raise BudgetExceeded("budget reached")
                    record = {
                        "kind": "taste",
                        "world_id": args.world,
                        "archive_revision": archive.revision,
                        "world_clock": now.isoformat(),
                        **{k: v for k, v in item.items() if k != "context"},
                        "model": model,
                        "sample": i,
                        "line": line,
                        "abstained": line == ABSTAIN_TOKEN,
                        "error": error,
                        "usage": meter.last_usage,
                        "stop_reason": meter.last_stop_reason,
                        "block_types": meter.last_block_types,
                        "seconds": round(time.monotonic() - started, 2),
                    }
                    write_record(fh, record)
                    samples.setdefault(item["context_id"], []).append(record)
                    print(f"[{meter.calls}] {model} {item['context_id']} "
                          f"{line or error} spent~{meter.spent_yuan:.3f}", file=sys.stderr)
        except BudgetExceeded as e:
            print(f"stopped: {e}", file=sys.stderr)
    if args.sheet:
        write_blind_sheet(args.sheet, contexts, samples, models, rng)
    print(f"calls={meter.calls} estimated_yuan={meter.spent_yuan:.4f}", file=sys.stderr)
    return 0


def write_blind_sheet(path, contexts, samples, models, rng):
    """A/B per context with the model hidden; the key goes to a separate file."""
    sheet, key = ["# MODEL-1 blind taste sheet", ""], {}
    for item in contexts:
        rows = samples.get(item["context_id"], [])
        if not rows:
            continue
        flip = rng.random() < 0.5
        label = {models[0]: "B" if flip else "A", models[1]: "A" if flip else "B"}
        key[item["context_id"]] = {v: k for k, v in label.items()}
        where = item["location_id"] or "-"
        sheet.append(f"## {item['context_id']}")
        sheet.append(
            f"location {where} · activity {item['activity']} · "
            f"co-located {', '.join(item['co_located']) or 'none'} · "
            f"online {', '.join(item['channel_peers']) or 'none'}"
        )
        for side in ("A", "B"):
            model = next(m for m, s in label.items() if s == side)
            sheet.append(f"\n**{side}**")
            for row in rows:
                if row["model"] == model:
                    sheet.append(f"- {row['line'] or '（失败：' + str(row['error']) + '）'}")
        sheet.append("\n偏好：A / B / 差不多　　备注：\n")
    Path(path).write_text("\n".join(sheet) + "\n", encoding="utf-8")
    Path(str(path) + ".key.json").write_text(
        json.dumps(key, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ── summary ────────────────────────────────────────────────────────────
def cmd_summary(args):
    rows = []
    with open(args.path, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    judge = [r for r in rows if r["kind"] == "judge"]
    taste = [r for r in rows if r["kind"] == "taste"]
    spent = sum(estimate_yuan(r.get("usage")) for r in rows)
    print(f"calls={len(rows)} estimated_yuan={spent:.4f}")
    if judge:
        summarize_judge(judge)
    if taste:
        summarize_taste(taste)
    return 0


def _usage_line(rows):
    def mean(field):
        values = [(r.get("usage") or {}).get(field) or 0 for r in rows]
        return sum(values) / len(values) if values else 0
    return (f"mean input={mean('input_tokens'):.0f} cache_read={mean('cache_read_input_tokens'):.0f} "
            f"output={mean('output_tokens'):.0f} yuan/call="
            f"{sum(estimate_yuan(r.get('usage')) for r in rows) / max(len(rows), 1):.4f}")


def summarize_judge(rows):
    by_model = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r)
    scores = {}  # (model, rep) -> {key: score}
    for r in rows:
        if not r["failed"]:
            scores.setdefault((r["model"], r["rep"]), {})[r["key"]] = r["drift_score"]
    original = {r["key"]: r["original"]["drift_score"] for r in rows}
    print(f"\njudge: {len(original)} lines, threshold {THRESHOLD}")
    for model, mrows in sorted(by_model.items()):
        ok = [r for r in mrows if not r["failed"]]
        vals = [r["drift_score"] for r in ok]
        print(f"- {model}: {len(mrows)} calls, {len(mrows) - len(ok)} failed, "
              f"mean score {sum(vals) / max(len(vals), 1):.2f}, "
              f"rejected {sum(v >= THRESHOLD for v in vals)}/{len(vals)}")
        print(f"  {_usage_line(mrows)}")
        print(f"  stop_reasons {dict(Counter(r.get('stop_reason') for r in mrows))} "
              f"blocks {dict(Counter(tuple(r.get('block_types') or ()) for r in mrows))}")
    runs = sorted(scores)
    print("\nverdict agreement (rejected = score >= threshold), pairwise:")
    labelled = [("archived", original)] + [(f"{m}#{rep}", scores[(m, rep)]) for m, rep in runs]
    for i, (la, a) in enumerate(labelled):
        for lb, b in labelled[i + 1:]:
            keys = sorted(set(a) & set(b))
            if not keys:
                continue
            flips = [k for k in keys if (a[k] >= THRESHOLD) != (b[k] >= THRESHOLD)]
            mad = sum(abs(a[k] - b[k]) for k in keys) / len(keys)
            print(f"- {la} vs {lb}: {len(flips)}/{len(keys)} verdict flips "
                  f"({len(flips) / len(keys):.0%}), mean |diff| {mad:.2f}")


def summarize_taste(rows):
    print(f"\ntaste: {len({r['context_id'] for r in rows})} contexts")
    by_model = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r)
    for model, mrows in sorted(by_model.items()):
        print(f"- {model}: {len(mrows)} calls, {sum(bool(r['error']) for r in mrows)} errors, "
              f"{sum(r['abstained'] for r in mrows)} abstained")
        print(f"  {_usage_line(mrows)}")
        print(f"  stop_reasons {dict(Counter(r.get('stop_reason') for r in mrows))} "
              f"blocks {dict(Counter(tuple(r.get('block_types') or ()) for r in mrows))}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    j = sub.add_parser("judge")
    j.add_argument("--world-root", required=True)
    j.add_argument("--world", required=True)
    j.add_argument("--runs", default="mimo-v2.5-pro:2,mimo-v2.6-pro:1")
    j.add_argument("--min-score", type=float, default=3.0)
    j.add_argument("--limit", type=int)
    j.add_argument("--budget-yuan", type=float, required=True)
    j.add_argument("--seed", type=int, default=1)
    j.add_argument("--out", required=True)
    j.add_argument("--dry-run", action="store_true")
    j.set_defaults(func=cmd_judge)

    t = sub.add_parser("taste")
    t.add_argument("--world-root", required=True)
    t.add_argument("--world", required=True)
    t.add_argument("--models", default="mimo-v2.5-pro,mimo-v2.6-pro")
    t.add_argument("--residents")
    t.add_argument("--samples", type=int, default=3)
    t.add_argument("--budget-yuan", type=float, required=True)
    t.add_argument("--seed", type=int, default=1)
    t.add_argument("--out", required=True)
    t.add_argument("--sheet")
    t.add_argument("--dry-run", action="store_true")
    t.set_defaults(func=cmd_taste)

    s = sub.add_parser("summary")
    s.add_argument("path")
    s.set_defaults(func=cmd_summary)

    args = parser.parse_args(argv)
    if getattr(args, "budget_yuan", 1) <= 0:
        parser.error("--budget-yuan must be positive")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
