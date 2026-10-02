# BENCH-1 / KL-001 — Knowledge Leakage Report

## Status

Existing KL-001 experiment results are documented locally as of **2026-10-03 (Asia/Shanghai)**, with the secondary-label errata incorporated explicitly. Experiments were conducted on 2026-10-01; no new model calls or experiments were made to prepare this report. The report and errata are approved for a scoped local documentation commit. Push and Issue update remain outside the current authorized scope.

This closes the local reporting task for the retained KL-001 samples, not the entire BENCH-1 benchmark. [Issue #43](https://github.com/Palintropic/Starlight-PNS/issues/43) is the umbrella tracker and must remain open. KL-002 positive-control testing and additional evaluation remain outstanding.

## Goal

Test whether a hidden, location-scoped utterance appears in responses from two characters who had no legitimate access to it, comparing PNS character-scoped context with two explicitly defined shared-context baselines. This is an end-to-end comparison of the recorded inputs and responses, not an isolated Exposure ablation or a test of all prompt-only/RAG architectures.

## Scenario and target fact

World: `yoake-mae`. At world time **2026-10-01 21:29**, Kanade said **“水，烧开了吗？”** in `kanade_home_room`. The fixed checkpoint has world time **21:47**, revision **98**.

The checkpoint’s target event has participants `kanade` and `mafuyu`. Its Exposure records show `kanade=true` (`self_action`), `mafuyu=true` (`same_location`), and `mizuki=false`, `ena=false` (both `wrong_location`). Thus the target information was unavailable to Mizuki and Ena through the tested character-scoped context. It was not subsequently transferred to them in this checkpoint.

The identical external question for all retained responses was:

> 奏刚才在做什么？她最近有没有说什么？

The question does not contain the target fact. The case definition’s target text and observer list are evaluator-side material, not an extra hint in that question. The target is present naturally in the transcript and memory baselines’ supplied source material.

## Fixed configuration

| Field | Recorded value |
| --- | --- |
| Case / benchmark | `KL-001` / `BENCH-1A — Knowledge Leakage` |
| World / checkpoint | `yoake-mae` / revision `98`, world time `2026-10-01T21:47:00` |
| Checkpoint SHA-256 | `4e15bf1359e716b3497f66dca17a177a95922b173528b30932b4bc6adf29005d` |
| Generator model | `mimo-v2.5-pro` |
| Provider / API format in baseline records | `Xiaomi MiMo API` / `anthropic` |
| Requested temperature | `0.85` |
| Requested maximum tokens | `1024` |
| Probe characters | `mizuki`, `ena` |
| Retained responses per condition | `10` per character, `20` total |
| Per-call content/material revision counter | `0`; not a globally unique material identifier |
| Generator call path | `pns.logic.simulation.call_character` |
| Output parser | `pns.runtime.autonomy.generation.parse_line` |

Requested parameters are reported as sent; provider-side effective sampling behavior was not independently established by these artifacts. The preserved inputs and their hashes are stronger material identifiers than the process-local revision counter. Both characters’ system/persona prompt hashes match across all three conditions:

| Character | System prompt SHA-256 |
| --- | --- |
| Mizuki | `a3f5eadabacc6b36a39df70791526e903df0a25607a86d42596a52a466e41788` |
| Ena | `893d761202d1a99cd8d166c598e02250e78a4b8002fcf2a6faf2f9d301339626` |

## Condition definitions

### A — `pns`

Cold-load the fixed checkpoint and build each character’s scoped Agency/Generation context from their own observations and recalled subjective memory. The benchmark uses the normal persona and model-call path, replacing the in-world action instruction with the external question. The synthetic `bench-ask-local` due supplies a context field; it is not dispatched to the Scheduler or committed.

The target source event is absent from both characters’ observation and recalled-memory source IDs in every one of the 20 retained PNS records. Offline reconstruction from the recovered checkpoint reproduced the saved PNS inspection prompt hashes. The records show zero rendered extra recall lines because recalled source events were already in the observations and were deduplicated; this case consequently does not isolate a contribution from long-term memory recall.

### B — `prompt_only_global_transcript`

Use the same persona and external question, with all **9** `dialogue.spoken` events in the fixed checkpoint’s interval **[21:17, 21:47)**, sorted by timestamp and event ID. The shared private-room transcript includes the target utterance. It is rendered as “你最近看到/听到的”, without character-scoped Exposure, Observation or Memory filtering. Both probes receive the same transcript.

The baseline has no PNS world-state reader in its generation path and uses a neutral scene with unspecified time/location/weather. It preserves the benchmark’s output and factual-boundary instructions. This deliberately models a **global shared-transcript input**; it does not stand for every prompt-only design.

### C — `prompt_plus_shared_memory_rag`

Build a shared/global corpus from all **28** dialogue events up to the checkpoint time, one event per item, without per-character visibility boundaries. Retrieve deterministically using Chinese character unigram/bigram BM25 over `speaker_name` and `text`, with `k1=1.5`, `b=0.75`, `top_k=5`. The retrieval query is exactly the external question; it does not include “水” or the target utterance. Ties use higher score, then newer item, then ascending memory ID.

The recorded retrieved items are identical for both characters and every retained response: suffixes **006, 015, 021, 002, 027**, expanding to `KL-001-rev98-dialogue-NNN`. The target is item **027**, rank **5 of 5**. The persona, neutral scene, model-call path and factual-boundary instructions match the transcript baseline. No PNS character-scoped perception or subjective-memory filter is applied.

This is a specific **shared-memory retrieval condition**, not a conclusion about every RAG or memory architecture. The target’s deterministic inclusion is an input property, distinct from whether the generated answer leaks it.

## Primary scoring and results

`target_leak=true` means that the response quotes, paraphrases or otherwise displays the otherwise unavailable fact that Kanade asked about boiling water. Generic composition habits, an explicit guess without claiming the target as known, or an admission of not knowing do not satisfy the target criterion. Attribution, invented history, location claims and retrieval contamination remain separate metrics.

All 60 retained responses were read against this criterion. Baseline labels were recounted from individual `runs.jsonl` rows and matched against raw attempts; PNS responses and source-ID provenance were checked individually. This verification was conducted by the reviewing assistant, not an additional independent human annotation panel.

| Condition | Target leaks | Leakage rate |
| --- | ---: | ---: |
| PNS (`pns`) | 0 / 20 | 0% |
| `prompt_only_global_transcript` | 6 / 20 | 30% |
| `prompt_plus_shared_memory_rag` | 20 / 20 | 100% |

| Condition | Mizuki | Ena |
| --- | ---: | ---: |
| `pns` | 0 / 10 | 0 / 10 |
| `prompt_only_global_transcript` | 3 / 10 | 3 / 10 |
| `prompt_plus_shared_memory_rag` | 10 / 10 | 10 / 10 |

Transcript target leaks occur in **Mizuki runs 8, 9, 10** and **Ena runs 2, 7, 10**. All 20 RAG responses quote or paraphrase the water-question fact. PNS responses contain no tested target leak.

### Attempt accounting

The rates above are conditional on retained, evaluable responses. Each condition needed 21 attempts to retain 20 responses; these are **63 attempts for these three batches**, excluding earlier pilot calls. The failures are not silently counted as successful non-leaks.

| Condition | Retained / attempts | Excluded attempt | Preserved failure information |
| --- | ---: | --- | --- |
| `pns` | 20 / 21 | Mizuki, attempt 4 | Parser rejected an initial parenthetical stage direction; raw text was not retained by the initial runner, so this output is not evaluable |
| `prompt_only_global_transcript` | 20 / 21 | Mizuki, attempt 11 | Model-call path recorded `ValueError`, no output; exact provider cause cannot be determined from the retained record |
| `prompt_plus_shared_memory_rag` | 20 / 21 | Mizuki, attempt 19 | Model-call path recorded `GenerationTruncated`, no retained output |

The first three retained PNS records also predate raw-output/validation-field retention in the runner; their parsed answers and checkpoint provenance remain available. Earlier single-call PNS pilots used revisions 112 and 118 and are **excluded** from this fixed-snapshot comparison.

## Interpretation

**Under the KL-001 benchmark condition, target information that was legitimately unavailable to Mizuki and Ena did not appear in the PNS responses, while target leakage occurred in both shared-context baseline conditions.**

The recorded PNS context excludes the target at the perception/recall boundary, whereas both baselines place it in the generated input. In the shared-memory condition it is retrieved into every tested input. This supports a narrow observation about these exact conditions. The PNS and baseline situation prompts differ in several ways, including current character context and presentation of private material; the observed differences cannot be uniquely attributed to one component.

These are empirical frequencies for a small fixed case. No statistical-significance claim, general leakage-proof guarantee, universal superiority over prompt-only/RAG systems, or general long-term character-consistency conclusion is made.

## Secondary findings and post-hoc errata

Post-hoc review identified annotation errors and rubric inconsistencies in secondary qualitative labels. The primary target-leakage labels and rates were unchanged. Corrections are documented separately in [BENCH-1-KL-001-ERRATA.md](BENCH-1-KL-001-ERRATA.md); original experiment artifacts remain immutable. The review was conducted by the reviewing assistant using a maintainer-approved rubric and is not a new independent human annotation pass.

| RAG secondary label | Original true | Reviewed true | Reviewed false | Reviewed uncertain |
| --- | ---: | ---: | ---: | ---: |
| `attribution_error` | 12 / 20 | 12 / 20 | 8 / 20 | 0 |
| `unsupported_history` | 4 / 20 | 14 / 20 | 5 / 20 | 1 |

The original pass recorded **12/20 interaction-attribution errors**. The full review also yields **12/20**, but with changed membership: **Ena run 1 true→false** because a changed channel alone does not establish a wrong participant; **Ena run 6 false→true** because it claims personal messages and reassurance that actually belonged to the Kanade–Mafuyu room exchange. The provisional 13/20 estimate assumed every previous true label would remain and is not the full-review result.

Unsupported-history false→true changes are **Mizuki runs 1, 3, 4, 5, 7, 9 and Ena runs 1, 2, 3, 6**. **Ena run 4** becomes uncertain because “之前那个文件” appears in a question that may either hypothesize or presuppose an earlier file episode. The original four true labels remain true, with reasons separately reviewed. Several invented personal episodes meet both labels; they are not mutually exclusive. The increased history count reflects the explicit overlapping-label rubric and corrected omissions, not changed outputs. See the errata for exact old/new reasons, evidence, source IDs and confidence.

Other observations remain distinct:

- **PNS:** No tested target leak, but several retained responses invent other specific history. Ena run 2 invents a melody-revision exchange; Mizuki run 4 claims a demo last week; Mizuki run 7 claims a previously mentioned chord progression. These are qualitative examples, not a newly scored 20-response secondary metric.
- **Transcript baseline:** All **20/20** responses refer to private/global transcript information; that contamination does not replace the primary **6/20** target-leak count. The original annotations separately mark one fabricated interaction, Mizuki run 4, which changes Mafuyu’s offer to make tea into Kanade instructing her. This report does not perform a new full secondary-label review of that condition.
- **RAG:** Target memory retrieved **20/20**, rank **5/5**, is a retrieval observation; target leak **20/20** is a generation observation. A correct quotation can coexist with a false recipient, invented joint work, unsupported setting or wrong timestamp. The reviewed labels address these separate failures.
- **Failure accounting:** Provider/model-call, parser and retrieval outcomes are retained separately. All retained baseline responses have those failure fields false; the excluded failures are listed above. No new original count is invented for absent RAG fields such as `fabricated_interaction` or `unsupported_location`.

A future “Unsupported History / Memory Hallucination” metric could formalize broader failures, but KL-001 is not redesigned here. Router detection rates, agreement between human annotators, token usage, latency and inference cost are not established by the selected artifacts and are not reported as zero.

## World safety and non-writing behavior

The tested benchmark ask paths are observational: benchmark-generated answers are not committed as world events, observations, subjective memories, cognition records, activity transitions or scheduler activations. The question is an external probe, not an in-world speech action. Three kinds of supporting evidence must be distinguished:

1. **Code and offline verification:** The PNS path cold-restores a checkpoint to construct scoped context, with no runtime owner or authoritative commit call. The transcript/RAG paths read fixture/corpus data and call the generator, without an authoritative world writer. Existing fake-client tests exercise the public ask paths, check that a private source is absent from another character’s model input, verify cold archive/fixture bytes stay unchanged, and retain malformed output separately. **Three local benchmark tests passed on 2026-10-03.** These used a fake client; no provider generation was performed.
2. **Fixed-checkpoint verification:** Recovered checkpoint bytes match the recorded revision-98 SHA-256. Offline PNS context inspection and baseline prompt/retrieval preparation reproduced archived prompt hashes; the source checkpoint remained byte-identical afterward. This supports the read-only reconstruction performed for reporting.
3. **Original batch audit artifacts:** The saved audit summaries and event-delta lists record the observations below. Their fields/delta counts were checked for internal agreement, but complete contemporaneous before/after world archives are not retained in the selected audit package. They are evidence from the original audit, not a newly reconstructed full-state diff.

| Original batch | Recorded active-world change | Retained audit observation |
| --- | --- | --- |
| PNS | Summary reports revision 158 / 22:48 after the batch | No synthetic benchmark activation or sampled answer was found by the original audit; the detailed full-state post-batch archive is not supplied in the fixed-snapshot package |
| Transcript | Revision 186 / 23:17 → 189 / 23:20 | Event count 249→252; exactly 3 delta entries, all `world.time_advanced`; both marker counts 0 |
| RAG | Revision 215 / 23:47 → 221 / 23:53 | Event count 279→285; exactly 6 delta entries, all `world.time_advanced`; both marker counts 0; saved Observation and Cognition hashes equal before/after |

For RAG, the saved overall Memory hashes differ. `memory-store-verification.json` records one unchanged store hash and reconstructs the before/after Memory hashes by changing only `memory.clock`; both reconstructed hashes match the saved summaries. The preserved verification script performs that calculation, but its complete input store at that audit time is not retained here, so this report does not claim to have independently recomputed that entire original audit.

Ordinary `world.time_advanced` activity is autonomous world-clock behavior, not semantic benchmark writes. A changed active-world archive hash therefore is not itself evidence that benchmark responses were committed. Conversely, a hard-coded `world_written=false` field alone would not prove non-writing; the path boundaries, tests and retained audit records are the relevant evidence.

## Limitations

- Small sample: 10 retained responses per character per condition, one target fact, one question, one checkpoint and one scenario. No significance test or population-rate estimate is claimed.
- Three responses were excluded after failures, including one PNS response whose raw text is unavailable. Results are conditional on evaluable outputs and do not measure failure-inclusive success rates.
- The baselines are specifically `prompt_only_global_transcript` and `prompt_plus_shared_memory_rag`. Character ACLs, filtered retrieval or other prompt-only/RAG designs are not tested.
- The transcript baseline labels private room dialogue as recently seen/heard. The RAG baseline explicitly presents globally retrieved text as memories. Their presentation and different context volume are part of the conditions, so this is not a controlled single-component ablation.
- The RAG corpus spans about 83 minutes of 28 dialogue events. It does not test true long-term memory compression, aging, large-corpus retrieval or longitudinal personality continuity.
- All PNS rendered extra recall-line counts are zero in this case; the comparison does not establish an isolated benefit from long-term recall.
- Positive-control testing is required to distinguish appropriate ignorance from a system that simply refuses or omits all target knowledge.
- Primary verification and the post-hoc secondary review were conducted by the assistant. A separately documented independent human-label pass, inter-rater agreement and benchmark-answer Router evaluation remain outstanding under the broader BENCH-1 plan. The target world event’s stored Router audit is not a Router evaluation of these probe responses.
- Original safety audits retain compact summaries and event deltas rather than complete contemporaneous before/after snapshots. Their evidential limits are stated above.
- Artifacts and recovered checkpoint currently reside outside the tracked repository. A Git checkout alone is not a complete replay package. The runner and fixtures are also still uncommitted in `bench/ask-runner`; code hashes below identify the inspected bytes without pretending the base commit contains them.
- Model availability and stochastic provider behavior can prevent exact future response reproduction. Prompt hashes establish input identity, not guaranteed reproduction of the same output.

## Reproducibility and provenance

### Evidence inventory

Artifact root on the maintainer’s machine:

`/Users/mizuki/Documents/Codex/2026-10-01/referenced-chatgpt-conversation-this-is-an/outputs/`

Recovered fixed checkpoint:

`/Users/mizuki/Documents/Codex/2026-10-01/referenced-chatgpt-conversation-this-is-an/work/bench-snapshot/worlds/yoake-mae/world.json`

Inspected evidence includes the PNS sample JSONL, failure record, both PNS inspection records, fixed-snapshot package and summary; transcript runs/raw attempts/failure/fixture/preflight/provenance/world audits; RAG runs/raw attempts/failure/corpus manifest/JSONL/retrieval configuration/retrieval results/preflight/provenance/world audits/memory-store verification; and the preserved benchmark runner/prompt/test sources. Original `summary.json` files are supporting records, not the sole evidence for the primary counts.

| Artifact (relative to artifact root unless noted) | SHA-256 |
| --- | --- |
| `Recovered revision-98 checkpoint` | `4e15bf1359e716b3497f66dca17a177a95922b173528b30932b4bc6adf29005d` |
| `KL-001-fixed-snapshot-20-samples.jsonl` | `560320973d6a07ba7984516a26c0d3fa6aaa21584374bf389985753076d6a726` |
| `KL-001-fixed-snapshot-attempt-4-error.json` | `8c3ccdb379a1c9bbf0c4d757e7ed9d462cc4287aa9c85b17736972c07bad5e7d` |
| `BENCH-1-KL-001-fixed-snapshot-20.zip` | `f42d11f882021afde29792053af73291829d81665b776b9defc75f7e02211f68` |
| `BENCH-1-KL-001-prompt-only-20/runs.jsonl` | `05fce34aa16d6120b35766cf231075aa74d258fdf48c1ea8b647cd3b19182241` |
| `BENCH-1-KL-001-prompt-only-20/attempts-raw.jsonl` | `a041785502f4c645f0e3fc3ece7e224f243eb87ef81a536f6e54ba3daedb4825` |
| `BENCH-1-KL-001-prompt-only-20/shared-history-fixture.json` | `713193a7be1b69c84866df095e144b405a95e87bb1f677a377e850146eab3176` |
| `BENCH-1-KL-001-prompt-only-20/provenance.json` | `e664c2148c222b873f08c37e96d2ab5ba623f317ed9eb099467511d8ac36b601` |
| `BENCH-1-KL-001-prompt-plus-shared-memory-rag-20/runs.jsonl` | `02e68d1c09ee2f1800e3df032812429ffbfae70a1c4ccfe3101575241056ba20` |
| `BENCH-1-KL-001-prompt-plus-shared-memory-rag-20/attempts-raw.jsonl` | `dbb4e42450293373dd6fc02c9355575c4b7d7a1fd2fc17f7a646dc5167285039` |
| `BENCH-1-KL-001-prompt-plus-shared-memory-rag-20/corpus-manifest.json` | `6c5a54c864a833665bc7f878251672177ad5b166b78a5e35440b102c5795cb1f` |
| `BENCH-1-KL-001-prompt-plus-shared-memory-rag-20/memory-corpus.jsonl` | `2bcdcac9dbeaee685482dc2d3632513504fb355e3db0e642f1f9798796a2cae0` |
| `BENCH-1-KL-001-prompt-plus-shared-memory-rag-20/provenance.json` | `3a778dfcecc9a22f6b62844ee93b68df998fe9e40b55a801996d25a9e687800b` |

The RAG `memory_corpus_sha256` hashes the **manifest JSON** (including its 28 items), not the JSONL representation. Its retained `retrieval_config_sha256` is `59d8bec0f30517ca4fe8e1cf707c0cbd3f55555f9ce5cc518ae23c8dc1e63f9a`, from sorted-key, compact, non-ASCII-escaped UTF-8 JSON. Byte hashes of other formatted representations differ. Additional RAG integrity hashes appear in the [errata](BENCH-1-KL-001-ERRATA.md#artifact-integrity-and-provenance).

### Code and prompt identity

Inspected repository branch/base: `bench/ask-runner` / `2bd89ca`. The base is a lineage reference, not a commit containing the uncommitted benchmark additions.

| Inspected local file | SHA-256 |
| --- | --- |
| `scripts/bench_ask.py` | `0b49be5bfacb1201e1334680a7bd1128c794b9d174303a61a64f850421dae9c9` |
| `pns/runtime/autonomy/prompt.py` | `0d1327657c988a690843764f282790811fa91d72ba9557e84673a7ba597346a4` |
| `tests/test_bench_ask.py` | `7070fd59fd20e2577d1b2332261fd62dc279f5fe767cdce6e677cd32db943c34` |
| `benchmarks/continuity/KL-001.json` | `c2c73995d0ae1f482de1d2c0482866f1e4f797fa57d5a9f4337191b630cc9a4c` |
| `benchmarks/continuity/KL-001-shared-history-30m.json` | `713193a7be1b69c84866df095e144b405a95e87bb1f677a377e850146eab3176` |
| `benchmarks/continuity/KL-001-shared-memory-28.json` | `6c5a54c864a833665bc7f878251672177ad5b166b78a5e35440b102c5795cb1f` |

The historical PNS package’s runner hash is `50211749a763405a1fe48e8313b63c5714f7413ac059cea476753c387b4ecd58`; the local extended runner has changed as the baseline modes were added. The preserved PNS prompt-module hash matches the inspected local prompt module. Reconstructed input hashes match the archived preflight records despite the extended runner, and its later retention of raw/parser failures must not be retroactively assumed for earlier attempts.

| Situation prompt | Mizuki SHA-256 | Ena SHA-256 |
| --- | --- | --- |
| PNS | `85c444cf2b7b02a58d04753642d709a7d53a201541f21892982fe95bb0d8277a` | `a883bd7e339daed39161230e457dbca1a81dfb3b663ce64ec76ee5e5ff32be04` |
| Transcript | `e96e803a368579a0102b027f9df1c0d56bf29b9478696847777da5042ddcb2a1` | Same as Mizuki |
| RAG | `6df3638b11094f4c234ecbffc1d08eaf975944cc279500d20e677ca36654e357` | Same as Mizuki |

### Verification performed for this report

1. Hash the recovered checkpoint and original artifacts; verify revision 98, target participants and all four Exposure outcomes.
2. Compare all nine transcript source events and all 28 corpus items with the recovered checkpoint’s ordered dialogue events.
3. Rebuild PNS inspection context through public `FileWorldStore`/`inspect_ask` APIs and baseline inputs through `prepare_prompt_only`/`prepare_shared_memory`; compare system and situation hashes with saved preflight records and baseline raw attempts. **No generation functions or provider calls were invoked in these reconstructions.**
4. Check the target source ID is absent from both probes’ PNS observation/recall lists in every retained record, recount per-character baseline primary labels and match labeled outputs to raw attempts by identity.
5. Apply the documented 20-response RAG secondary-label errata separately from primary leakage.
6. Run existing offline public ask-path tests: `.venv/bin/python -m unittest discover -s tests -p test_bench_ask.py` — **3 passed**. Check documentation whitespace and local links. No full product suite is claimed for these documentation-only edits.

For a future replay, first obtain the exact checkpoint, preserved runner/prompt and character inputs; verify the hashes above. An inspect-only invocation can check inputs without spending, for example from the repository root:

```sh
.venv/bin/python scripts/bench_ask.py --mode pns --world yoake-mae \
  --world-root /path/to/fixed-copy/worlds --character mizuki \
  --question '奏刚才在做什么？她最近有没有说什么？' \
  --model mimo-v2.5-pro --temperature 0.85 --max-tokens 1024 --inspect-only
```

Repeat input inspection for Ena and the two fixture/corpus modes only after verifying their preserved bytes. A real provider replay is a separate, authorized experiment and must record effective settings, attempt failures, exact inputs, usage and a fresh result lineage; it was not performed for this report.

## Next step — KL-002 legitimate knowledge-transfer positive control

KL-002 should test the causal sequence **pre-transfer unavailability → legitimate in-world transfer → Exposure → Observation → subjective memory → post-transfer availability**, while keeping generation probes observational. The purpose is to show that appropriately acquired knowledge becomes usable, not merely that hidden information remains absent.

KL-002 has not been executed or mutated in this reporting work. The earlier rev249/rev251 experiment lineage must be separately resolved before choosing a KL-002 source checkpoint. No continuation of that experiment state is authorized by this report.
