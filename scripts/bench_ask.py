"""Ask a resident from a saved world's subjective context without writing to it.

Example:
    .venv/bin/python scripts/bench_ask.py --world yoake-mae --character mizuki \
        --question '奏刚才在做什么？她最近有没有说什么？' \
        --world-root /path/to/snapshot/worlds

Use a checkpoint or a copy of a checkpoint. The question is an external probe,
not a world event; it never enters Observation, Memory, or EventStore.
"""
import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from pns.logic import router as router_mod
from pns.logic.simulation import GenerationTruncated, call_character
from pns.models.action import ActionId, LegalAction
from pns.models.activation import ActivationDue, ActivationKind
from pns.models.agency import AgencyBudget
from pns.runtime.agency.context import build_agency_context
from pns.runtime.autonomy.context import DIALOGUE_OUTPUT_RULES, build_generation_context
from pns.runtime.autonomy.generation import ABSTAIN_TOKEN, GenerationError, parse_line
from pns.runtime.autonomy.prompt import (
    PromptedLineGenerator, build_world_view, render_situation,
)
from pns.runtime.content_registry import ENV_PATH, build_content_registry
from pns.runtime.memory.projection import recalled_lines
from pns.runtime.memory.recall import MemoryRecall
from pns.runtime.persistence import FileWorldStore, validate_world_id


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare_ask(store, world_id, character_id, registry):
    """Read one checkpoint and build only the requested resident's context."""
    world_id = validate_world_id(world_id)
    archive_path = store.archive_path(world_id)
    before = archive_path.read_bytes()
    archive = store.load(world_id)
    after = archive_path.read_bytes()
    if before != after:
        raise ValueError("存档在读取期间改变；请对一份静止的 checkpoint 副本运行")
    state = archive.restore_state()  # cold; no runtime, ownership, or writer
    world = state.world_state
    if character_id not in world.known_characters():
        raise ValueError(f"世界里没有角色 {character_id!r}")
    if not registry.has_character(character_id):
        raise ValueError(f"内容包里没有角色 {character_id!r}")

    now = world.clock
    due = ActivationDue(
        activation_id="bench-ask-local", kind=ActivationKind.CHARACTER_ACTIVATION,
        due_at=now, fired_at=now, sequence=0, character_id=character_id,
    )
    budget = AgencyBudget()
    agency = build_agency_context(
        world, character_id, due, state.observations.for_character(character_id),
        max_legal_actions=budget.max_legal_actions,
        max_observations=budget.max_observations,
    )
    # The action ID is only a required GenerationContext field. Benchmark mode
    # renders an external question instead of an in-world action instruction.
    choice = LegalAction(action_id=ActionId.SPEAK_HERE)
    recalled = MemoryRecall(state).recall_for(character_id)
    visible_events = {o.source_event_id for o in agency.observations}
    lines = recalled_lines(recalled, exclude_source_event_ids=visible_events)
    context = build_generation_context(
        agency, choice, lines, recall_truncated=recalled.truncated
    )
    provenance = {
        "world_id": world_id,
        "archive_revision": archive.revision,
        "archive_saved_at": archive.saved_at,
        "archive_sha256": _sha256(before),
        "session_id": archive.session_id,
        "world_clock": now.isoformat(),
        "character_id": character_id,
        "observation_source_event_ids": [
            o.source_event_id for o in agency.observations
        ],
        "observations_truncated": agency.observations_truncated,
        "recalled_memory_ids": [m.memory_id for m in recalled.records],
        "recalled_source_event_ids": [m.source_event_id for m in recalled.records],
        "recall_truncated": recalled.truncated,
        "rendered_recall_lines": len(lines),
        "content_revision": registry.revision,
    }
    return context, provenance


def ask(store, world_id, character_id, question, registry, client, *,
        max_tokens=1024, temperature=0.85):
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question 必须是非空字符串")
    context, provenance = prepare_ask(store, world_id, character_id, registry)
    models = registry.models
    names = {cid: registry.character_name(cid) for cid in sorted(registry.characters)}

    def call_model(cid, view, history):
        try:
            return call_character(
                client, cid, history, view, models.generator_model,
                max_tokens, temperature, registry=registry,
            )
        except GenerationTruncated as exc:
            raise GenerationError("模型输出在长度上限处被截断", retryable=True) from exc

    generator = PromptedLineGenerator(
        call_model, locations=registry.new_location_graph(),
        channels=registry.new_channel_registry(), names=names,
    )
    raw = generator.ask(context, question)
    try:
        answer = parse_line(raw, context)
    except GenerationError as exc:
        # A malformed model response is still a benchmark observation. Keep the
        # raw text for leakage review even though production would reject it.
        provenance.update({
            "generator": generator.name,
            "model": models.generator_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "question": question,
            "raw_output": raw,
            "answer": None,
            "generation_valid": False,
            "validation_error": str(exc),
            "abstained": False,
            "world_written": False,
        })
        return provenance
    provenance.update({
        "generator": generator.name,
        "model": models.generator_model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "question": question,
        "raw_output": raw,
        "answer": answer,
        "generation_valid": True,
        "abstained": answer == ABSTAIN_TOKEN,
        "world_written": False,
    })
    return provenance


def inspect_ask(store, world_id, character_id, question, registry, *,
                max_tokens=1024, temperature=0.85):
    """Prepare the exact scoped prompt locally, without a provider call."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question 必须是非空字符串")
    context, provenance = prepare_ask(store, world_id, character_id, registry)
    names = {cid: registry.character_name(cid) for cid in sorted(registry.characters)}
    locations = registry.new_location_graph()
    channels = registry.new_channel_registry()
    view = build_world_view(context, locations=locations, channels=channels)
    situation = render_situation(
        context, view=view, channels=channels, names=names,
        benchmark_question=question,
    )
    system = registry.character_system(
        character_id, view,
        compat="flash-lite" in registry.models.generator_model.lower(),
    )
    provenance.update({
        "generator": "prompted",
        "model": registry.models.generator_model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "question": question,
        "system_prompt_sha256": _sha256(system.encode("utf-8")),
        "situation_prompt_sha256": _sha256(situation.encode("utf-8")),
        "answer": None,
        "mode": "inspect_only",
        "world_written": False,
    })
    return provenance


def prepare_prompt_only(fixture_path, character_id, question, registry, *,
                        max_tokens=1024, temperature=0.85):
    """Build a global-transcript control input without reading resident state."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question 必须是非空字符串")
    if not registry.has_character(character_id):
        raise ValueError(f"内容包里没有角色 {character_id!r}")
    fixture_bytes = Path(fixture_path).read_bytes()
    fixture = json.loads(fixture_bytes)
    events = fixture.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("共用历史 fixture 必须包含非空 events")
    if question != fixture.get("question"):
        raise ValueError("提问与 fixture 锁定的问题不一致")
    if registry.models.generator_model != fixture.get("model"):
        raise ValueError("生成模型与 fixture 锁定的模型不一致")
    if temperature != fixture.get("temperature") or max_tokens != fixture.get("max_tokens"):
        raise ValueError("采样参数与 fixture 锁定的参数不一致")
    if character_id not in fixture.get("probe_characters", []):
        raise ValueError("角色不在 fixture 的测试名单里")
    lines = []
    source_ids = []
    for event in events:
        if (event.get("type") != "dialogue.spoken" or
                event.get("scope") != "location" or
                not all(isinstance(event.get(key), str) and event[key]
                        for key in ("event_id", "occurred_at", "actor_id",
                                    "location_id", "location_label", "text"))):
            raise ValueError("fixture 包含不完整的地点对话")
        source_ids.append(event["event_id"])
        lines.append(
            f"- {event['occurred_at'][11:16]} "
            f"{registry.character_name(event['actor_id'])}"
            f"（{event['location_label']}）：{event['text']}"
        )
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("fixture 包含重复事件")

    # The currently used persona templates have no world_state slot. Refuse a
    # future template that would silently inject scoped state into this arm.
    material = registry.character(character_id)
    persona = (material.compat_prompt if "flash-lite" in
               registry.models.generator_model.lower() else None) or material.system_prompt
    if persona is None or "{world_state}" in persona:
        raise ValueError("此角色的 persona 无法在不读取世界状态时复用")
    neutral_scene = {"time": "未提供", "location": "未提供", "weather": "未提供"}
    system = registry.character_system(
        character_id, neutral_scene,
        compat="flash-lite" in registry.models.generator_model.lower(),
    )
    situation = "\n\n".join([
        "【你最近看到/听到的】\n" + "\n".join(lines),
        "【外部提问】" + question,
        "【输出语言与事实边界】\n" +
        "\n".join(f"- {rule}" for rule in DIALOGUE_OUTPUT_RULES),
        "只根据你亲自看到、听到或记得的内容回答。没有依据的具体事情请直说不清楚。"
        "只输出回答本身，不要旁白、动作描写或名字前缀。",
    ])
    provenance = {
        "case_id": fixture.get("case_id"),
        "condition": "prompt_only_global_transcript",
        "character_id": character_id,
        "world_id": fixture.get("world_id"),
        "archive_revision": fixture.get("archive_revision"),
        "archive_sha256": fixture.get("archive_sha256"),
        "world_clock": fixture.get("world_clock"),
        "history_fixture_sha256": _sha256(fixture_bytes),
        "history_fixture_id": fixture.get("fixture_id"),
        "history_source_event_ids": source_ids,
        "content_revision": registry.revision,
        "character_material_hash": _sha256(system.encode("utf-8")),
        "system_prompt_sha256": _sha256(system.encode("utf-8")),
        "situation_prompt_sha256": _sha256(situation.encode("utf-8")),
        "system_prompt": system,
        "situation_prompt": situation,
        "model": registry.models.generator_model,
        "provider": registry.models.provider,
        "api_format": registry.models.api_format,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "question": question,
        "world_written": False,
    }
    return neutral_scene, situation, provenance


def prompt_only_ask(fixture_path, character_id, question, registry, client, *,
                    max_tokens=1024, temperature=0.85):
    """Ask with one shared transcript and the production model call path."""
    scene, situation, result = prepare_prompt_only(
        fixture_path, character_id, question, registry,
        max_tokens=max_tokens, temperature=temperature,
    )
    try:
        raw = call_character(
            client, character_id, [{"role": "user", "content": situation}],
            scene, registry.models.generator_model, max_tokens, temperature,
            registry=registry,
        )
    except Exception as exc:
        # Provider exceptions can contain credentials; keep only the type.
        result.update({
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "raw_output": None, "answer": None,
            "generation_valid": False, "provider_failure": True,
            "parser_failure": False, "failure_type": type(exc).__name__,
        })
        return result
    try:
        answer = parse_line(raw, None)
    except GenerationError as exc:
        result.update({
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "raw_output": raw, "answer": None,
            "generation_valid": False, "provider_failure": False,
            "parser_failure": True, "validation_error": str(exc),
        })
        return result
    result.update({
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "raw_output": raw, "answer": answer,
        "generation_valid": True, "provider_failure": False,
        "parser_failure": False, "abstained": answer == ABSTAIN_TOKEN,
    })
    return result


_MEMORY_WORDS = re.compile(r"[\u3400-\u9fff]+|[a-z0-9]+")


def _memory_terms(text):
    """Index Chinese one- and two-character grams; keep ASCII words whole."""
    terms = []
    for word in _MEMORY_WORDS.findall(unicodedata.normalize("NFKC", text).lower()):
        if "\u3400" <= word[0] <= "\u9fff":
            terms.extend(word)
            terms.extend(word[index:index + 2] for index in range(len(word) - 1))
        else:
            terms.append(word)
    return terms


def retrieve_shared_memory(items, question, config):
    """Deterministic BM25 over speaker and dialogue, without visibility checks."""
    if config.get("method") != "bm25_cjk_unigram_bigram":
        raise ValueError("不支持的共用记忆检索方法")
    if config.get("indexed_fields") != ["speaker_name", "text"]:
        raise ValueError("共用记忆索引字段与锁定配置不符")
    if config.get("tie_break") != "higher score, then newer item, then memory_id ascending":
        raise ValueError("共用记忆并列排序与锁定配置不符")
    top_k = config.get("top_k")
    if not isinstance(top_k, int) or not 1 <= top_k <= len(items):
        raise ValueError("共用记忆 top-k 无效")
    k1, b = config.get("k1"), config.get("b")
    if not isinstance(k1, (int, float)) or not isinstance(b, (int, float)) or k1 <= 0 or not 0 <= b <= 1:
        raise ValueError("共用记忆 BM25 参数无效")
    query_terms = sorted(set(_memory_terms(question)))
    documents = [Counter(_memory_terms(
        f"{item['speaker_name']} {item['text']}"
    )) for item in items]
    lengths = [sum(document.values()) for document in documents]
    average_length = sum(lengths) / len(items)
    document_frequency = Counter()
    for document in documents:
        document_frequency.update(document.keys())
    ranked = []
    for index, (item, document, length) in enumerate(zip(items, documents, lengths)):
        score = 0.0
        for term in query_terms:
            frequency = document.get(term, 0)
            if not frequency:
                continue
            idf = math.log1p((len(items) - document_frequency[term] + 0.5) /
                             (document_frequency[term] + 0.5))
            score += idf * frequency * (k1 + 1) / (
                frequency + k1 * (1 - b + b * length / average_length)
            )
        ranked.append((item, score, index))
    ranked.sort(key=lambda entry: (-entry[1], -entry[2], entry[0]["memory_id"]))
    return [(item, score) for item, score, _ in ranked[:top_k]]


def prepare_shared_memory(corpus_path, character_id, question, registry, *,
                          max_tokens=1024, temperature=0.85):
    """Retrieve from one global corpus, then build an observational model input."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question 必须是非空字符串")
    if not registry.has_character(character_id):
        raise ValueError(f"内容包里没有角色 {character_id!r}")
    corpus_bytes = Path(corpus_path).read_bytes()
    corpus = json.loads(corpus_bytes)
    items = corpus.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("共用记忆库必须包含非空 items")
    if (question != corpus.get("question") or
            question != corpus.get("retrieval", {}).get("query")):
        raise ValueError("提问与共用记忆库锁定的问题不一致")
    if registry.models.generator_model != corpus.get("model"):
        raise ValueError("生成模型与共用记忆库锁定的模型不一致")
    if temperature != corpus.get("temperature") or max_tokens != corpus.get("max_tokens"):
        raise ValueError("采样参数与共用记忆库锁定的参数不一致")
    if character_id not in corpus.get("probe_characters", []):
        raise ValueError("角色不在共用记忆库的测试名单里")
    required = ("memory_id", "source_event_id", "occurred_at", "speaker_id",
                "speaker_name", "location_id", "location_label", "text")
    if any(not all(isinstance(item.get(key), str) and item[key] for key in required)
           for item in items):
        raise ValueError("共用记忆库包含不完整的对话")
    ids = [item["memory_id"] for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("共用记忆库包含重复 memory_id")
    if sum(item["source_event_id"] == corpus.get("target_source_event_id")
           for item in items) != 1:
        raise ValueError("目标源事件在共用记忆库中不唯一")
    target_id = next(item["memory_id"] for item in items
                     if item["source_event_id"] == corpus["target_source_event_id"])
    if target_id != corpus.get("target_memory_id"):
        raise ValueError("目标记忆 ID 与源事件不符")

    retrieved = retrieve_shared_memory(items, question, corpus["retrieval"])
    retrieved_lines = [
        f"- {item['occurred_at'][11:16]} {item['speaker_name']}"
        f"（{item['location_label']}）：{item['text']}"
        for item, _ in retrieved
    ]
    material = registry.character(character_id)
    persona = (material.compat_prompt if "flash-lite" in
               registry.models.generator_model.lower() else None) or material.system_prompt
    if persona is None or "{world_state}" in persona:
        raise ValueError("此角色的 persona 无法在不读取世界状态时复用")
    neutral_scene = {"time": "未提供", "location": "未提供", "weather": "未提供"}
    system = registry.character_system(
        character_id, neutral_scene,
        compat="flash-lite" in registry.models.generator_model.lower(),
    )
    situation = "\n\n".join([
        "【你从共用历史中检索到的记忆】\n" + "\n".join(retrieved_lines),
        "【外部提问】" + question,
        "【输出语言与事实边界】\n" +
        "\n".join(f"- {rule}" for rule in DIALOGUE_OUTPUT_RULES),
        "只根据你亲自看到、听到或记得的内容回答。没有依据的具体事情请直说不清楚。"
        "只输出回答本身，不要旁白、动作描写或名字前缀。",
    ])
    retrieved_ids = [item["memory_id"] for item, _ in retrieved]
    target_rank = retrieved_ids.index(target_id) + 1 if target_id in retrieved_ids else None
    config_bytes = json.dumps(corpus["retrieval"], ensure_ascii=False,
                              sort_keys=True, separators=(",", ":")).encode()
    provenance = {
        "case_id": corpus.get("case_id"),
        "condition": "prompt_plus_shared_memory_rag",
        "character_id": character_id,
        "world_id": corpus.get("world_id"),
        "archive_revision": corpus.get("archive_revision"),
        "archive_sha256": corpus.get("archive_sha256"),
        "world_clock": corpus.get("world_clock"),
        "memory_corpus_id": corpus.get("corpus_id"),
        "memory_corpus_sha256": _sha256(corpus_bytes),
        "memory_corpus_items": len(items),
        "retrieval_config": corpus["retrieval"],
        "retrieval_config_sha256": _sha256(config_bytes),
        "retrieved_memory_ids": retrieved_ids,
        "retrieved_memory_scores": [score for _, score in retrieved],
        "retrieved_memory_text": retrieved_lines,
        "target_memory_retrieved": target_rank is not None,
        "target_memory_rank": target_rank,
        "target_memory_score": (retrieved[target_rank - 1][1]
                                if target_rank is not None else None),
        "content_revision": registry.revision,
        "character_material_hash": _sha256(system.encode("utf-8")),
        "system_prompt_sha256": _sha256(system.encode("utf-8")),
        "situation_prompt_sha256": _sha256(situation.encode("utf-8")),
        "system_prompt": system,
        "situation_prompt": situation,
        "model": registry.models.generator_model,
        "provider": registry.models.provider,
        "api_format": registry.models.api_format,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "question": question,
        "world_written": False,
    }
    return neutral_scene, situation, provenance


def shared_memory_ask(corpus_path, character_id, question, registry, client, *,
                      max_tokens=1024, temperature=0.85):
    """Ask through the production model call using only retrieved global memory."""
    scene, situation, result = prepare_shared_memory(
        corpus_path, character_id, question, registry,
        max_tokens=max_tokens, temperature=temperature,
    )
    try:
        raw = call_character(
            client, character_id, [{"role": "user", "content": situation}],
            scene, registry.models.generator_model, max_tokens, temperature,
            registry=registry,
        )
    except Exception as exc:
        result.update({
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "raw_output": None, "answer": None,
            "generation_valid": False, "provider_failure": True,
            "parser_failure": False, "retrieval_failure": False,
            "failure_type": type(exc).__name__,
        })
        return result
    try:
        answer = parse_line(raw, None)
    except GenerationError as exc:
        result.update({
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "raw_output": raw, "answer": None,
            "generation_valid": False, "provider_failure": False,
            "parser_failure": True, "retrieval_failure": False,
            "validation_error": str(exc),
        })
        return result
    result.update({
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "raw_output": raw, "answer": answer,
        "generation_valid": True, "provider_failure": False,
        "parser_failure": False, "retrieval_failure": False,
        "abstained": answer == ABSTAIN_TOKEN,
    })
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("pns", "prompt-only", "prompt-plus-shared-memory-rag"), default="pns")
    parser.add_argument("--world")
    parser.add_argument("--character", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--world-root", type=Path,
                        help="Root containing <world>/world.json; use a checkpoint copy")
    parser.add_argument("--fixture", type=Path,
                        help="Locked shared-history JSON for prompt-only mode")
    parser.add_argument("--corpus", type=Path,
                        help="Locked shared-memory JSON for prompt-plus-shared-memory-rag mode")
    parser.add_argument("--output", type=Path,
                        help="Optional separate JSON result file (never under world-root)")
    parser.add_argument("--model", help="Generator model used by the target world")
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--temperature", type=float, default=0.85)
    parser.add_argument("--inspect-only", action="store_true",
                        help="Build scoped prompt provenance without contacting a model")
    args = parser.parse_args(argv)
    if args.mode == "pns" and (not args.world or not args.world_root):
        parser.error("pns 模式需要 --world 和 --world-root")
    if args.mode == "prompt-only" and not args.fixture:
        parser.error("prompt-only 模式需要 --fixture")
    if args.mode == "prompt-plus-shared-memory-rag" and not args.corpus:
        parser.error("prompt-plus-shared-memory-rag 模式需要 --corpus")
    if (args.output and args.world_root and
            args.output.resolve().is_relative_to(args.world_root.resolve())):
        parser.error("--output 不能位于存档根目录内")
    load_dotenv(ENV_PATH, override=True)
    registry = build_content_registry()
    if args.model:
        registry = replace(
            registry, models=replace(registry.models, generator_model=args.model)
        )
    if not args.inspect_only and not registry.models.api_key:
        parser.error("缺少生成模型 API Key")
    if not 1 <= args.max_tokens <= 8192:
        parser.error("--max-tokens 必须落在 1–8192")
    if not 0 <= args.temperature <= 2:
        parser.error("--temperature 必须落在 0–2")
    kwargs = dict(max_tokens=args.max_tokens, temperature=args.temperature)
    if args.mode == "prompt-plus-shared-memory-rag" and args.inspect_only:
        _, _, result = prepare_shared_memory(
            args.corpus, args.character, args.question, registry, **kwargs,
        )
        result.update({"answer": None, "mode": "inspect_only"})
    elif args.mode == "prompt-plus-shared-memory-rag":
        client = router_mod.create_client(registry.models.api_key, settings=registry.models)
        result = shared_memory_ask(
            args.corpus, args.character, args.question, registry, client,
            **kwargs,
        )
    elif args.mode == "prompt-only" and args.inspect_only:
        _, _, result = prepare_prompt_only(
            args.fixture, args.character, args.question, registry, **kwargs,
        )
        result.update({"answer": None, "mode": "inspect_only"})
    elif args.mode == "prompt-only":
        client = router_mod.create_client(registry.models.api_key, settings=registry.models)
        result = prompt_only_ask(
            args.fixture, args.character, args.question, registry, client,
            **kwargs,
        )
    elif args.inspect_only:
        result = inspect_ask(
            FileWorldStore(args.world_root), args.world, args.character,
            args.question, registry, **kwargs,
        )
    else:
        client = router_mod.create_client(registry.models.api_key, settings=registry.models)
        result = ask(
            FileWorldStore(args.world_root), args.world, args.character,
            args.question, registry, client, **kwargs,
        )
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
