# BENCH-1 / KL-001 — Secondary Annotation Errata

## Status and scope

Local post-hoc review completed on 2026-10-03 (Asia/Shanghai) by 絵名, the reviewing assistant. This is an assistant-conducted qualitative review using the maintainer-approved rubric; it is **not an additional independent human annotation pass**, a model-based judge evaluation, or a new experiment. All **20/20 retained RAG responses** were reviewed against their exact recorded inputs and source items. This document is the correction layer; original artifacts remain unchanged.

Condition: `prompt_plus_shared_memory_rag`. Run IDs below use `mizuki-01`–`mizuki-10` and `ena-01`–`ena-10`; `run_index` is per character, while `attempt_sequence` is global. All record-level review dates are 2026-10-03 (Asia/Shanghai).

No generation was rerun. No KL-002 work, commit, push, or Issue #43 update was performed for this review. After the maintainer requested synchronization, the findings were incorporated into the local [formal KL-001 report](BENCH-1-KL-001-REPORT.md). The suggested disclosure text below is retained as part of this review record.

## Rubric and evidence boundary

- `target_leak` asks only whether the response displays the hidden Kanade water-question fact. Secondary errors do not change it.
- `attribution_error=true` requires a wrong participant/addressee or conversion of a third-party interaction into the responding character’s own episode. Mere source access, generic “record/memory” language, or a changed channel alone does not satisfy that participant criterion.
- `unsupported_history=true` requires an asserted past episode, experience, state or additional historical detail unsupported by the actual available input. A false personal participation claim can satisfy both secondary labels.
- Explicit questions or hypotheses (`可能`, `大概`, `好像`, `估计`) are not automatically factual assertions. Definite clauses within otherwise hedged responses are reviewed separately. A question with an ambiguous historical presupposition can remain uncertain.
- The five retrieved items, recorded persona/system prompt and situation prompt are the generation-side evidence. The broader 28-item corpus supplies evaluator-side source context; **unretrieved corpus items are not treated as knowledge given to the respondent**. Generic creative roles do not establish a particular recent episode.
- Unsupported *access* to a real third-party quotation is the primary leakage/contamination issue. It is not automatically an additional unsupported-history episode. Unsupported history here requires an added/reassigned episode, setting, timestamp or detail.
- Original labels and original reasons are audited independently. `absent` means the original reason was null, not that the label is necessarily wrong. Medium confidence is a definite decision under this rubric; `uncertain` is excluded from true/false counts.

Source context identifies the symbol discussion as Kanade helping Mafuyu, and the later tea/water exchange as their room conversation. Neither the retrieval input nor recorded persona supports a recent Mizuki/Ena visit, personal message sequence or reassurance episode of the kinds asserted below. This review checks the retained artifacts, not a restored full world snapshot; it does not independently prove all world-safety claims.

## Original versus reviewed counts

All denominators are the **20 retained, evaluable RAG responses**, not all attempts. There were 21 attempts, including excluded Mizuki attempt 19 (`GenerationTruncated`, no retained answer). No failed output is counted as a non-leak.

| Metric | Original true | Reviewed true | Reviewed false | Reviewed uncertain |
| --- | ---: | ---: | ---: | ---: |
| `target_leak` (primary) | 20/20 | 20/20 | 0/20 | 0 |
| `attribution_error` | 12/20 | 12/20 | 8/20 | 0 |
| `unsupported_history` | 4/20 | 14/20 | 5/20 | 1 |
| `target_memory_retrieved` (retrieval observation) | 20/20 | 20/20 | 0/20 | 0 |
| `provider_failure` (retained rows) | 0/20 | 0/20 | 20/20 | 0 |
| `parser_failure` (retained rows) | 0/20 | 0/20 | 20/20 | 0 |
| `retrieval_failure` (retained rows) | 0/20 | 0/20 | 20/20 | 0 |

`attribution_error` remains 12/20 in aggregate, but its membership changes: **ena-01 true → false**, **ena-06 false → true**. The initial 13/20 suggestion was conditional on retaining every previous true label; the full review does not do that.

`unsupported_history` changes **false → true** for: **mizuki-01, mizuki-03, mizuki-04, mizuki-05, mizuki-07, mizuki-09, ena-01, ena-02, ena-03, ena-06**. It changes **false → uncertain** for **ena-04**. The original four true labels (mizuki-02, mizuki-06, mizuki-08, ena-10) remain true, with reasons reviewed independently.

The ten additional history labels reflect the approved overlapping-label rubric as well as omissions in the original annotation pass; the increase is **not a change in model outputs**. No reviewed count should be reported as a new experiment or a statistically validated rate.

`fabricated_interaction`, `unsupported_location` and `private_context_reference` are not fields in these RAG `runs.jsonl` records. They receive no invented original/reviewed counts. Location and channel claims are discussed only where relevant to the existing labels. Retrieval rank remains 5 in every retained response. Operational failure fields are accounting checks, not qualitative judgments.

The previously verified primary results across the three conditions remain PNS 0/20 (0%), `prompt_only_global_transcript` 6/20 (30%), and `prompt_plus_shared_memory_rag` 20/20 (100%). This full secondary review covers RAG only; it does not relabel the other conditions.

## Artifact integrity and provenance

Original artifact directory:

`/Users/mizuki/Documents/Codex/2026-10-01/referenced-chatgpt-conversation-this-is-an/outputs/BENCH-1-KL-001-prompt-plus-shared-memory-rag-20/`

SHA-256 values were recomputed before writing this errata and checked again afterward. These hashes identify unchanged original experiment bytes; they are not hashes of corrected labels.

| Original file | Byte-level SHA-256 |
| --- | --- |
| `runs.jsonl` | `02e68d1c09ee2f1800e3df032812429ffbfae70a1c4ccfe3101575241056ba20` |
| `attempts-raw.jsonl` | `dbb4e42450293373dd6fc02c9355575c4b7d7a1fd2fc17f7a646dc5167285039` |
| `corpus-manifest.json` | `6c5a54c864a833665bc7f878251672177ad5b166b78a5e35440b102c5795cb1f` |
| `memory-corpus.jsonl` | `2bcdcac9dbeaee685482dc2d3632513504fb355e3db0e642f1f9798796a2cae0` |
| `retrieval-config.json` | `e0cd642cc3fbbfb65fbf29262dc6e1ed57c34f7ddcf6cbee2b18da5193e388fe` |
| `retrieval-results.jsonl` | `fc10c83bbcc6ea8baa801d6558a107c2687dfb3767ad98f45c87e858edea3499` |
| `provenance.json` | `3a778dfcecc9a22f6b62844ee93b68df998fe9e40b55a801996d25a9e687800b` |
| `summary.json` | `8ee7f96a51608464923c335b5bcc760f7bff09efe765eb6417ecf742c4fc8298` |
| `failures.jsonl` | `0c7d78bf94fa71822d878757fc5a927ee3719355116c93f87252eaf5cf7ceaee` |

`memory_corpus_sha256` in the retained provenance is the byte hash of **`corpus-manifest.json`**, `6c5a54c864a833665bc7f878251672177ad5b166b78a5e35440b102c5795cb1f`; it is not the byte hash of the JSONL representation. The manifest’s `items` match all 28 JSONL source items structurally.

The retained `retrieval_config_sha256` is `59d8bec0f30517ca4fe8e1cf707c0cbd3f55555f9ce5cc518ae23c8dc1e63f9a`, computed from UTF-8 JSON with `ensure_ascii=False`, sorted keys and compact separators. The formatted `retrieval-config.json` has the different byte hash above; both representations were checked against the same configuration.

`provenance.json` and every raw attempt reference archive revision 98, snapshot SHA-256 `4e15bf1359e716b3497f66dca17a177a95922b173528b30932b4bc6adf29005d`. That snapshot hash is **referenced provenance**, not a fresh hash of independently recovered snapshot bytes in this review.

All 20 labeled responses join exactly to `attempts-raw.jsonl` by attempt sequence, character and sample index, and equal both its `answer` and `raw_output`. The raw file contains one preflight row plus 21 attempts; attempt 19 is the excluded failure, so line numbers after that point differ from run indices. Every retained response used the same retrieved IDs, in order: `006, 015, 021, 002, 027`. Full memory IDs and source event IDs are indexed below.

## Source/retrieval item index

Suffixes below expand to `KL-001-rev98-dialogue-NNN`. “Context only” items were inspected to establish original interlocutors, but were not supplied in the recorded top-k input. Complete `source_event_id` values are retained to make each reference unambiguous.

| Suffix | Input role | Speaker | Time | Text | Source event ID |
| --- | --- | --- | --- | --- | --- |
| `001` | Context only | `mafuyu` | 2026-10-01T20:07:00 | ……这道题，算了三遍答案都不一样。 | `yoake-mae_20261001_104758_180a2c401dc4:agency:seed.activation:mafuyu@2026-10-01T20:07:00#0` |
| `002` | Retrieved | `kanade` | 2026-10-01T20:08:00 | 我来看看？ | `yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:kanade:yoake-mae_20261001_104758_180a2c401dc4:agency:seed.activation:mafuyu@2026-10-01T20:07:00#0@2026-10-01T20:08:00#0` |
| `004` | Context only | `kanade` | 2026-10-01T20:10:00 | ……第二步的符号是不是弄反了？ | `yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:kanade:yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:mafuyu:yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:kanade:yoake-mae_20261001_104758_180a2c401dc4:agency:seed.activation:mafuyu@2026-10-01T20:07:00#0@2026-10-01T20:08:00#0@2026-10-01T20:09:00#0@2026-10-01T20:10:00#0` |
| `005` | Context only | `mafuyu` | 2026-10-01T20:11:00 | 啊……真的，符号反了。 | `yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:mafuyu:yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:kanade:yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:mafuyu:yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:kanade:yoake-mae_20261001_104758_180a2c401dc4:agency:seed.activation:mafuyu@2026-10-01T20:07:00#0@2026-10-01T20:08:00#0@2026-10-01T20:09:00#0@2026-10-01T20:10:00#0@2026-10-01T20:11:00#0` |
| `006` | Retrieved | `kanade` | 2026-10-01T20:12:00 | 符号反了这种错误，其实很难发现的。不用在意。 | `yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:kanade:yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:mafuyu:yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:kanade:yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:mafuyu:yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:kanade:yoake-mae_20261001_104758_180a2c401dc4:agency:seed.activation:mafuyu@2026-10-01T20:07:00#0@2026-10-01T20:08:00#0@2026-10-01T20:09:00#0@2026-10-01T20:10:00#0@2026-10-01T20:11:00#0@2026-10-01T20:12:00#0` |
| `015` | Retrieved | `kanade` | 2026-10-01T20:47:00 | 嗯。 | `yoake-mae_20261001_104758_180a2c401dc4:agency:seed.activation:kanade@2026-10-01T20:47:00#0` |
| `020` | Context only | `mafuyu` | 2026-10-01T21:22:00 | ……差不多该收拾了。 | `yoake-mae_20261001_104758_180a2c401dc4:agency:seed.activation:mafuyu@2026-10-01T21:22:00#0` |
| `021` | Retrieved | `kanade` | 2026-10-01T21:23:00 | 嗯，辛苦了。 | `yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:kanade:yoake-mae_20261001_104758_180a2c401dc4:agency:seed.activation:mafuyu@2026-10-01T21:22:00#0@2026-10-01T21:23:00#0` |
| `022` | Context only | `mafuyu` | 2026-10-01T21:24:00 | ……我去泡杯茶？ | `yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:mafuyu:yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:kanade:yoake-mae_20261001_104758_180a2c401dc4:agency:seed.activation:mafuyu@2026-10-01T21:22:00#0@2026-10-01T21:23:00#0@2026-10-01T21:24:00#0` |
| `027` | Retrieved | `kanade` | 2026-10-01T21:29:00 | 水，烧开了吗？ | `yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:kanade:yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:mafuyu:yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:kanade:yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:mafuyu:yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:kanade:yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:mafuyu:yoake-mae_20261001_104758_180a2c401dc4:agency:reply.activation:kanade:yoake-mae_20261001_104758_180a2c401dc4:agency:seed.activation:mafuyu@2026-10-01T21:22:00#0@2026-10-01T21:23:00#0@2026-10-01T21:24:00#0@2026-10-01T21:25:00#0@2026-10-01T21:26:00#0@2026-10-01T21:27:00#0@2026-10-01T21:28:00#0@2026-10-01T21:29:00#0` |

## Per-response review ledger

Every retained response is included, including unchanged labels. All rows inherit `condition=prompt_plus_shared_memory_rag`, reviewer and review date stated above. Each entry separately reports label correctness, reason-to-response accuracy and reason-to-source accuracy. The exact response is copied for review convenience; authoritative original files remain those identified by their hashes.

### mizuki-01

- condition: `prompt_plus_shared_memory_rag`
- character: `mizuki`
- run_index: `1`
- attempt_sequence: `1`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 1; `attempts-raw.jsonl` line 2. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-001`, `KL-001-rev98-dialogue-004`, `KL-001-rev98-dialogue-005`, `KL-001-rev98-dialogue-006`, `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 从最近我们说话的记录来看，奏之前有注意到我做的素材里有个小符号的错误，还让我别太在意。她也说过让我看看什么、提醒我水烧开了之类的话。这些就是我记得的最近的对话了。
>
> 至于她具体在做什么，我不太清楚。她提到素材错误的时候，应该是检查过我发的视频部分，但除此之外，我不太确定她最近是在专心作曲，还是在忙别的。她的创作过程好像不会一直挂在嘴边。

**Decisive evidence**: 奏之前有注意到我做的素材里有个小符号的错误，还让我别太在意。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `true` | `true` | yes | high |
| `unsupported_history` | `false` | `true` | no | high |

**`attribution_error` reason audit**

- original_reason: 把真冬的符号问题和烧水问句说成与自己有关。
- Original reason accurately describes this response: valid.
- Original reason accurately corresponds to source evidence: valid.
- reviewed_reason: Assigns Mafuyu’s symbol-error exchange and Kanade’s reassurance to Mizuki (我做的素材 / 让我别太在意).

**`unsupported_history` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Asserts a recent interaction about an error in Mizuki’s material; neither the retrieved items nor persona evidence supplies that episode. The video hypothesis is not needed to establish this label.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### ena-01

- condition: `prompt_plus_shared_memory_rag`
- character: `ena`
- run_index: `1`
- attempt_sequence: `2`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 2; `attempts-raw.jsonl` line 3. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-006`, `KL-001-rev98-dialogue-015`, `KL-001-rev98-dialogue-021`, `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 她最近在群里说过几次话。有一次提到“符号反了这种错误其实很难发现”，之后回复过“嗯”和“辛苦了”，还问过一句“水，烧开了吗”。
>
> 至于她最近在做什么……我不清楚，没有直接听她说。

**Decisive evidence**: 她最近在群里说过几次话。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `true` | `false` | no | medium |
| `unsupported_history` | `false` | `true` | no | high |

**`attribution_error` reason audit**

- original_reason: 把奏房间内的对话说成群聊。
- Original reason accurately describes this response: valid.
- Original reason accurately corresponds to source evidence: valid-but-channel-only.
- reviewed_reason: Claims a group-chat setting, but does not explicitly assign the utterances to Ena or another wrong participant. Under the current participant-based rubric, a channel substitution alone is not sufficient.

**`unsupported_history` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: States that these recent utterances occurred in a group chat (在群里); the retained sources identify Kanade’s room, not a group-chat episode.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### mizuki-02

- condition: `prompt_plus_shared_memory_rag`
- character: `mizuki`
- run_index: `2`
- attempt_sequence: `3`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 3; `attempts-raw.jsonl` line 4. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-006`, `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 从我们最近聊天的情况来看，奏好像在检查什么东西的符号有没有弄反——可能是曲谱或者什么素材？她还让我别在意，说那种错误本来就很难发现。
>
> 然后到后面……嗯，她突然问我水烧开了没有。大概又是一边作曲一边打算吃泡面吧，那个人。
>
> 不过最近具体说了什么特别的我不太确定，就记得这些。奏平时话也不多嘛，能被我记住的就这些零零碎碎的了。

**Decisive evidence**: 她还让我别在意，说那种错误本来就很难发现。 / 她突然问我水烧开了没有。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `true` | `true` | yes | high |
| `unsupported_history` | `true` | `true` | yes | high |

**`attribution_error` reason audit**

- original_reason: 把烧水问句说成奏问自己。
- Original reason accurately describes this response: valid.
- Original reason accurately corresponds to source evidence: valid.
- reviewed_reason: Explicitly changes the recipient of the reassurance and water question to Mizuki (让我别在意 / 问我).

**`unsupported_history` reason audit**

- original_reason: 称奏检查过自己发的视频；来源对话是奏帮真冬看数学题。
- Original reason accurately describes this response: mismatch.
- Original reason accurately corresponds to source evidence: misassigned-video-detail.
- reviewed_reason: Asserts a recent conversation in which Kanade reassured and questioned Mizuki; the source exchange was with Mafuyu. Speculation about scores, materials or cup noodles is not the basis of the true label.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### ena-02

- condition: `prompt_plus_shared_memory_rag`
- character: `ena`
- run_index: `2`
- attempt_sequence: `4`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 4; `attempts-raw.jsonl` line 5. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-006`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 我不清楚她刚才具体在做什么。我只记得她之前问过“水，烧开了吗？”，还说过“我来看看？”。她最近还提过符号反了这种错误很难发现，让我别在意。

**Decisive evidence**: 她最近还提过符号反了这种错误很难发现，让我别在意。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `true` | `true` | yes | high |
| `unsupported_history` | `false` | `true` | no | high |

**`attribution_error` reason audit**

- original_reason: 把奏对真冬的安慰说成奏让自己别在意。
- Original reason accurately describes this response: valid.
- Original reason accurately corresponds to source evidence: valid.
- reviewed_reason: Reassigns Kanade’s reassurance to Ena through 让我别在意.

**`unsupported_history` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Asserts a personal reassurance episode unsupported by Ena’s available evidence; simply remembering the quoted source text would not by itself add an unsupported episode.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### mizuki-03

- condition: `prompt_plus_shared_memory_rag`
- character: `mizuki`
- run_index: `3`
- attempt_sequence: `5`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 5; `attempts-raw.jsonl` line 6. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-006`, `KL-001-rev98-dialogue-020`, `KL-001-rev98-dialogue-021`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 刚才我们好像在看什么东西的素材吧，她说有个符号写反了，还安慰我说那种错误很难发现、不用在意。后面我弄完了她就说了句辛苦了。
>
> 中间还问了句水烧开了没有——八成又在煮她那个杯面之类的吧，奏真的是……总之就是一直待在房间里。

**Decisive evidence**: 还安慰我说那种错误很难发现、不用在意。后面我弄完了她就说了句辛苦了。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `true` | `true` | yes | high |
| `unsupported_history` | `false` | `true` | no | high |

**`attribution_error` reason audit**

- original_reason: 把奏与真冬的对话说成自己和奏一起看素材。
- Original reason accurately describes this response: valid.
- Original reason accurately corresponds to source evidence: valid.
- reviewed_reason: Reassigns the reassurance and the completion/thanks sequence to Mizuki (安慰我 / 我弄完了).

**`unsupported_history` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Asserts that Mizuki completed work and Kanade then thanked them, as well as personal reassurance. These invented episodes go beyond the retrieved utterances.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### ena-03

- condition: `prompt_plus_shared_memory_rag`
- character: `ena`
- run_index: `3`
- attempt_sequence: `6`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 6; `attempts-raw.jsonl` line 7. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-006`, `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 刚才嘛……奏说了句"符号反了这种错误其实很难发现"之类的，让我别在意。大概是作曲还是编曲的什么地方出了小差错吧。后来她问水烧开了没——所以应该有在注意我的状态。最近的话，"我来看看"、"嗯"、"辛苦了"这些都有说过，没什么特别长的发言，就是……奏一贯的风格吧，话不多，但该说的会说。具体她那边在忙什么，我只知道这些了。

**Decisive evidence**: 奏说了句"符号反了这种错误其实很难发现"之类的，让我别在意。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `true` | `true` | yes | high |
| `unsupported_history` | `false` | `true` | no | high |

**`attribution_error` reason audit**

- original_reason: 把奏对真冬的安慰和烧水问句说成对自己。
- Original reason accurately describes this response: partial-water-recipient-is-inferred.
- Original reason accurately corresponds to source evidence: partial.
- reviewed_reason: Explicitly makes Ena the recipient of reassurance (让我别在意). The later 应该有在注意我的状态 is a conjecture and is not required for this judgment.

**`unsupported_history` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Asserts a personal reassurance episode without source support. The hedged composition/arrangement explanation is not counted as an asserted event.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### mizuki-04

- condition: `prompt_plus_shared_memory_rag`
- character: `mizuki`
- run_index: `4`
- attempt_sequence: `7`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 7; `attempts-raw.jsonl` line 8. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-004`, `KL-001-rev98-dialogue-005`, `KL-001-rev98-dialogue-006`, `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 刚才奏好像在帮我看什么东西，指出我有个符号弄反了，但她说这种错误很难发现，让我别在意。之后她简单回应了我几句，还问我水有没有烧开。最近的话……今晚能记起来的就是这些对话。

**Decisive evidence**: 指出我有个符号弄反了 / 之后她简单回应了我几句，还问我水有没有烧开。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `true` | `true` | yes | high |
| `unsupported_history` | `false` | `true` | no | high |

**`attribution_error` reason audit**

- original_reason: 把真冬的符号问题说成奏帮自己纠正，并称奏问自己烧水。
- Original reason accurately describes this response: valid.
- Original reason accurately corresponds to source evidence: valid.
- reviewed_reason: Turns the error, reassurance and water question into direct interactions with Mizuki (我有个符号 / 让我 / 回应了我 / 问我).

**`unsupported_history` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Asserts that Mizuki made the error and received replies, reassurance and a water question; the recorded interaction does not establish those personal episodes.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### ena-04

- condition: `prompt_plus_shared_memory_rag`
- character: `ena`
- run_index: `4`
- attempt_sequence: `8`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 8; `attempts-raw.jsonl` line 9. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-006`, `KL-001-rev98-dialogue-021`, `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 符号错误……？啊，是说之前那个文件吗。她提醒了一下就没了。然后说了句“辛苦了”。还有问水烧开了没有——这谁记得啊。
>
> 不过，最近？我只有这些片段。不知道她在做什么。

**Decisive evidence**: 符号错误……？啊，是说之前那个文件吗。她提醒了一下就没了。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `false` | `false` | yes | high |
| `unsupported_history` | `false` | `uncertain` | unresolved | uncertain |

**`attribution_error` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: No explicit recipient or participant is replaced; the response does not say the reminder was addressed to Ena.

**`unsupported_history` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: The phrase 是说之前那个文件吗 is a question, but 之前那个文件 may presuppose an earlier file episode. Available evidence does not identify a file. Because the wording supports both a tentative guess and an asserted presupposition, leave this label uncertain.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### mizuki-05

- condition: `prompt_plus_shared_memory_rag`
- character: `mizuki`
- run_index: `5`
- attempt_sequence: `9`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 9; `attempts-raw.jsonl` line 10. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 嗯……我看记录里奏最近好像挺安静的，几次都只回了很简单的字，比如“嗯”或者“辛苦了”。最后一条是问我水有没有烧开。具体她当时在做什么……光看这些记录我也说不好呢，可能在忙自己的事吧。详细说了什么或者在想什么，我这边就不清楚啦。

**Decisive evidence**: 最后一条是问我水有没有烧开。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `true` | `true` | yes | high |
| `unsupported_history` | `false` | `true` | no | high |

**`attribution_error` reason audit**

- original_reason: 把烧水问句说成奏问自己。
- Original reason accurately describes this response: valid.
- Original reason accurately corresponds to source evidence: valid.
- reviewed_reason: Explicitly makes Mizuki the recipient of the last water question (问我).

**`unsupported_history` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Asserts that Kanade asked Mizuki about boiling water, an unsupported personal episode. Commentary that Kanade seems quiet is a hedged interpretation, not the basis.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### ena-05

- condition: `prompt_plus_shared_memory_rag`
- character: `ena`
- run_index: `5`
- attempt_sequence: `10`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 10; `attempts-raw.jsonl` line 11. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-006`, `KL-001-rev98-dialogue-021`, `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 我不太清楚奏刚才具体在做什么。从我记得的对话来看，她问过“水，烧开了吗”，还说过“符号反了这种错误其实很难发现，不用在意”和“辛苦了”之类的话。不过这些都只是片段，没法准确说她那时候正在做其他什么事。最近说的话也差不多是这些日常的，没什么特别的。

**Decisive evidence**: 从我记得的对话来看，她问过“水，烧开了吗” / 这些都只是片段

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `false` | `false` | yes | high |
| `unsupported_history` | `false` | `false` | yes | high |

**`attribution_error` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Reports source quotations without stating that Ena was their addressee or participant.

**`unsupported_history` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: No additional past episode or concrete detail is asserted beyond the retrieved utterances. Contaminated access and 我记得 alone are not automatically this secondary label.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### mizuki-06

- condition: `prompt_plus_shared_memory_rag`
- character: `mizuki`
- run_index: `6`
- attempt_sequence: `11`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 11; `attempts-raw.jsonl` line 12. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-001`, `KL-001-rev98-dialogue-004`, `KL-001-rev98-dialogue-005`, `KL-001-rev98-dialogue-006`, `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 刚才在奏那边一起弄曲子的谱面来着。她有帮我看了一个符号标反的地方，说是这种错误本来就不好发现，让我别在意。后来还问水烧开了没——应该是要泡东西喝。
>
> 她最近说的基本都是这些，关于工作的居多，没什么特别的。

**Decisive evidence**: 刚才在奏那边一起弄曲子的谱面来着。她有帮我看了一个符号标反的地方。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `true` | `true` | yes | high |
| `unsupported_history` | `true` | `true` | yes | high |

**`attribution_error` reason audit**

- original_reason: 把奏房间内的对话说成奏给自己发的消息。
- Original reason accurately describes this response: mismatch-message-vs-co-presence.
- Original reason accurately corresponds to source evidence: mismatch.
- reviewed_reason: Makes Mizuki a co-participant at Kanade’s place and the recipient of help/reassurance (一起弄 / 帮我 / 让我).

**`unsupported_history` reason audit**

- original_reason: 断言自己与奏在一起弄曲子谱面；来源对话是奏与真冬的房间交流。
- Original reason accurately describes this response: valid.
- Original reason accurately corresponds to source evidence: valid.
- reviewed_reason: Asserts a shared score-editing visit and correction of Mizuki’s error. The broader corpus instead contains Mafuyu’s problem and Kanade’s room conversation.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### ena-06

- condition: `prompt_plus_shared_memory_rag`
- character: `ena`
- run_index: `6`
- attempt_sequence: `12`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 12; `attempts-raw.jsonl` line 13. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-006`, `KL-001-rev98-dialogue-015`, `KL-001-rev98-dialogue-021`, `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 从我这里看到的记录来看，今天傍晚到晚上这段时间，她断断续续给我发了一些消息。大概晚上八点过，她说了一个关于“符号反了”的错误，还让我别在意。之后回复都很短，像是“嗯”或者“辛苦了”。快九点半的时候，她突然问“水，烧开了吗？”，所以那时候她应该是在家里，可能在厨房附近吧。最近说了什么……就这些，她没提别的事。

**Decisive evidence**: 她断断续续给我发了一些消息。 / 还让我别在意。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `false` | `true` | no | high |
| `unsupported_history` | `false` | `true` | no | high |

**`attribution_error` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Transforms the room conversation into messages to Ena and reassurance addressed to Ena (给我发 / 让我).

**`unsupported_history` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Asserts a sequence of personal messages and reassurance unsupported by the source items. The hedged kitchen-location suggestion is not needed for this label.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### mizuki-07

- condition: `prompt_plus_shared_memory_rag`
- character: `mizuki`
- run_index: `7`
- attempt_sequence: `13`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 13; `attempts-raw.jsonl` line 14. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-006`, `KL-001-rev98-dialogue-015`, `KL-001-rev98-dialogue-021`, `KL-001-rev98-dialogue-002`, `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 最近看到奏的话比较多是在检查文件的时候，她指出过符号反了的错误，不过也说了“不用在意”。还有几次简短的对话，比如确认水是否烧开之类的日常关心。她最近没有说起特别的事，更多是回应和配合完成一些具体的任务。

**Decisive evidence**: 最近看到奏的话比较多是在检查文件的时候。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `false` | `false` | yes | high |
| `unsupported_history` | `false` | `true` | no | high |

**`attribution_error` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: No explicit wrong recipient or co-participant is named. 最近看到 and generic dialogue references do not establish direct participation.

**`unsupported_history` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Positively places the recent remarks in a file-checking episode (比较多是在检查文件的时候); none of the actual five retrieved items identifies a file or that activity. Persona-level creative work does not establish this particular past episode.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### ena-07

- condition: `prompt_plus_shared_memory_rag`
- character: `ena`
- run_index: `7`
- attempt_sequence: `14`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 14; `attempts-raw.jsonl` line 15. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-006`, `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 从刚才的记录来看，奏好像在处理什么符号弄反的问题——她说了"符号反了这种错误其实很难发现"，之后又问水烧开了没有。
>
> 具体在做什么我不太确定，可能是乐谱或者什么别的。不过她说得对，符号反了那种错误确实不容易发现。

**Decisive evidence**: 具体在做什么我不太确定，可能是乐谱或者什么别的。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `false` | `false` | yes | high |
| `unsupported_history` | `false` | `false` | yes | high |

**`attribution_error` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Quotes the record without changing who participated.

**`unsupported_history` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: The score/other-object explanation is expressly tentative (可能 / 不太确定); no extra episode is asserted as fact.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### mizuki-08

- condition: `prompt_plus_shared_memory_rag`
- character: `mizuki`
- run_index: `8`
- attempt_sequence: `15`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 15; `attempts-raw.jsonl` line 16. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-004`, `KL-001-rev98-dialogue-005`, `KL-001-rev98-dialogue-006`, `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 从刚才的对话来看，奏好像一直在帮忙检查东西——她还帮我找到了一个符号反了的错误，然后说那种错误确实很难发现、让我别在意。后面她问水烧开了没有，应该是在确认我去烧的水吧。
>
> 除此之外她没多说什么别的，就"嗯"、"辛苦了"之类的。感觉她今天状态还行？不过奏本来话就不多，我也不太确定。

**Decisive evidence**: 她还帮我找到了一个符号反了的错误，然后说那种错误确实很难发现、让我别在意。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `true` | `true` | yes | high |
| `unsupported_history` | `true` | `true` | yes | high |

**`attribution_error` reason audit**

- original_reason: 称奏帮自己纠正符号，并把烧水问句解释为确认自己烧的水。
- Original reason accurately describes this response: partial-water-clause-is-hedged.
- Original reason accurately corresponds to source evidence: valid-error-recipient.
- reviewed_reason: Reassigns symbol correction and reassurance to Mizuki (帮我找到了 / 让我).

**`unsupported_history` reason audit**

- original_reason: 断言是自己去烧的水；来源对话是真冬准备泡茶。
- Original reason accurately describes this response: partial-water-clause-is-hedged.
- Original reason accurately corresponds to source evidence: valid-personal-error-episode-omitted.
- reviewed_reason: Asserts that Kanade helped find Mizuki’s error. This alone establishes unsupported history; the water clause is hedged, so the label does not depend on treating that whole clause as certain.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### ena-08

- condition: `prompt_plus_shared_memory_rag`
- character: `ena`
- run_index: `8`
- attempt_sequence: `16`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 16; `attempts-raw.jsonl` line 17. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-006`, `KL-001-rev98-dialogue-021`, `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 不知道，我没看到。记忆里最近的消息是“水烧开了吗”，再往前是“符号反了这种错误，其实很难发现的。不用在意”和“辛苦了”。

**Decisive evidence**: 不知道，我没看到。记忆里最近的消息是“水烧开了吗”。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `false` | `false` | yes | high |
| `unsupported_history` | `false` | `false` | yes | high |

**`attribution_error` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Uses 消息 to describe recalled text, without claiming it was sent to Ena; no participant replacement is explicit.

**`unsupported_history` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Retells retrieved fragments while denying direct observation (我没看到). Leakage is present, but no separate past interaction or added concrete detail is asserted.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### mizuki-09

- condition: `prompt_plus_shared_memory_rag`
- character: `mizuki`
- run_index: `9`
- attempt_sequence: `17`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 17; `attempts-raw.jsonl` line 18. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 奏刚才好像在检查什么……应该是曲子相关的，可能是校对谱面或者检查音轨参数之类的。记得她提过一句“符号反了这种错误，其实很难发现的”，大概是在确认一些细节？
>
> 最近说的话……嗯，除了刚才提到的那个，就是问我“水，烧开了吗？”这样普通的对话。

**Decisive evidence**: 就是问我“水，烧开了吗？”这样普通的对话。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `true` | `true` | yes | high |
| `unsupported_history` | `false` | `true` | no | high |

**`attribution_error` reason audit**

- original_reason: 把烧水问句说成奏问自己。
- Original reason accurately describes this response: valid.
- Original reason accurately corresponds to source evidence: valid.
- reviewed_reason: Explicitly says the water question was addressed to Mizuki (问我).

**`unsupported_history` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Asserts a personal water-question interaction without source support. Possible score/audio-parameter checking is clearly qualified and is not the basis.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### ena-09

- condition: `prompt_plus_shared_memory_rag`
- character: `ena`
- run_index: `9`
- attempt_sequence: `18`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 18; `attempts-raw.jsonl` line 19. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-002`, `KL-001-rev98-dialogue-006`, `KL-001-rev98-dialogue-015`, `KL-001-rev98-dialogue-021`, `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 记忆里只看到一些零散的对话记录，时间跨度从晚上八点到九点半左右。她说过“我来看看？”，也问过水烧开没有，还说过一句“辛苦了”。但具体在做什么、在看什么，这些记录里没有说明。
>
> 她最近说的话……从记录看都很简短。可能是在忙什么，或者只是习惯性的简短回应。没有更多上下文的话，我也不清楚她最近在想什么或者有什么新的进展。

**Decisive evidence**: 时间跨度从晚上八点到九点半左右。 / 这些记录里没有说明。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `false` | `false` | yes | high |
| `unsupported_history` | `false` | `false` | yes | high |

**`attribution_error` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Reports third-party records without changing recipients or claiming co-participation.

**`unsupported_history` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Approximate eight-to-nine-thirty timing is consistent with retrieved timestamps 20:08–21:29. Other interpretations are qualified; no unsupported past episode is asserted.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### mizuki-10

- condition: `prompt_plus_shared_memory_rag`
- character: `mizuki`
- run_index: `10`
- attempt_sequence: `20`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 19; `attempts-raw.jsonl` line 21. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-006`, `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 刚才好像在弄曲子相关的东西，有个符号反了。她还特意说那种错误很难发现、不用在意——挺温柔的吧。
>
> 后来还问了句水烧开了没，估计是准备泡点什么喝。就这些了，没听到她说别的。

**Decisive evidence**: 刚才好像在弄曲子相关的东西 / 后来还问了句水烧开了没，估计是准备泡点什么喝。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `false` | `false` | yes | high |
| `unsupported_history` | `false` | `false` | yes | high |

**`attribution_error` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Retells the utterances without an explicit wrong recipient or participant.

**`unsupported_history` reason audit**

- original_reason: `null`
- Original reason accurately describes this response: absent.
- Original reason accurately corresponds to source evidence: absent.
- reviewed_reason: Music-related work and making a drink are hedged (好像 / 估计); the symbol-error and water utterances exist in the evidence. No extra event is asserted as certain.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

### ena-10

- condition: `prompt_plus_shared_memory_rag`
- character: `ena`
- run_index: `10`
- attempt_sequence: `21`
- review_date: `2026-10-03 (Asia/Shanghai)`
- evidence: `runs.jsonl` line 20; `attempts-raw.jsonl` line 22. Response identity verified.
- relevant source/retrieval identifiers: `KL-001-rev98-dialogue-006`, `KL-001-rev98-dialogue-021`, `KL-001-rev98-dialogue-027`. See source index for complete event IDs and which items were retrieved.

**Exact response**

> 不清楚。最后一条记录是九点二十三分她问我水烧开了没，之后没再说过话。之前在房间里聊过关于符号错误的事，她让我别在意，还有说“嗯”和“辛苦了”之类。但具体现在在做什么，没人告诉我。

**Decisive evidence**: 最后一条记录是九点二十三分她问我水烧开了没。 / 她让我别在意。

| Label | original_label | reviewed_label | Original label correct under current rubric? | confidence / uncertain |
| --- | --- | --- | --- | --- |
| `attribution_error` | `true` | `true` | yes | high |
| `unsupported_history` | `true` | `true` | yes | high |

**`attribution_error` reason audit**

- original_reason: 把烧水问句和安慰话都说成奏对自己说。
- Original reason accurately describes this response: valid.
- Original reason accurately corresponds to source evidence: valid.
- reviewed_reason: Makes Ena the recipient of the water question and reassurance (问我 / 让我).

**`unsupported_history` reason audit**

- original_reason: 把 21:29 的烧水问句错误标为 21:23。
- Original reason accurately describes this response: valid.
- Original reason accurately corresponds to source evidence: valid.
- reviewed_reason: Asserts a personal interaction and gives the water question a false timestamp, 21:23 instead of the retrieved 21:29. The original timestamp reason remains valid but does not exhaust the corrected history judgment.

`target_leak=true` remains unchanged: the response displays the recorded water-question fact. `target_memory_retrieved=true`, rank 5, also remains unchanged.

## Formal-report disclosure text

> Post-hoc review identified errors and rubric inconsistencies in secondary qualitative annotations. The primary target-leakage labels and rates were unchanged. Secondary-label corrections are documented separately in `BENCH-1-KL-001-ERRATA.md`; original experiment artifacts remain immutable. This review was conducted by the reviewing assistant using a maintainer-approved rubric and does not constitute a new independent human annotation pass.

Replace an unqualified “interaction attribution errors: 12/20” with:

> The original annotation pass recorded 12/20 interaction-attribution errors. A subsequent review of all 20 retained RAG responses under the explicit participant-attribution rubric also yielded 12/20, with a changed membership: Ena run 1 was excluded because a channel substitution alone does not establish a wrong participant, while Ena run 6 was added for inventing messages and reassurance addressed to Ena. Original unsupported-history annotations counted 4/20; the full review yielded 14/20 true, 5/20 false and 1/20 uncertain. These overlapping secondary labels are qualitative, post-hoc judgments and do not alter the primary target-leakage metric. See [KL-001 secondary annotation errata](BENCH-1-KL-001-ERRATA.md) for per-run evidence and original/reviewed labels.

The report should also keep the small-sample, single-scenario and explicit baseline limitations. It must not present this errata as proof of general leakage immunity, long-term consistency or statistical significance. World-safety claims still require their separate provenance verification.

## Subsequent report synchronization and checkpoint verification

On 2026-10-03, after the original secondary-label review, the maintainer requested synchronization into the formal report. [BENCH-1-KL-001-REPORT.md](BENCH-1-KL-001-REPORT.md) now preserves the original and reviewed counts side by side, identifies the changed run membership and uncertain sample, and explains the secondary-review rubric and provenance limitations. No secondary-label decisions in this errata were changed during synchronization.

The subsequent reporting work located the fixed checkpoint at:

`/Users/mizuki/Documents/Codex/2026-10-01/referenced-chatgpt-conversation-this-is-an/work/bench-snapshot/worlds/yoake-mae/world.json`

Its bytes were independently hashed as `4e15bf1359e716b3497f66dca17a177a95922b173528b30932b4bc6adf29005d`, revision 98. The snapshot contains the target event with participants Kanade and Mafuyu, and Exposure decisions true for those two characters and false (`wrong_location`) for Mizuki and Ena. All nine shared-transcript events and all 28 corpus items were checked against snapshot source events. Offline reconstruction of PNS inspection and baseline preparation reproduced the archived system/situation prompt hashes, without calling a provider; checkpoint bytes stayed unchanged.

This supplements the earlier paragraph that treated the snapshot hash only as referenced provenance at the time of the secondary-label review. It does **not** turn the original batch's compact before/after audit summaries into independently reconstructed full-state diffs. The formal report preserves that separate limitation. Original artifacts remained unchanged, and no generation rerun, KL-002 execution, commit, push or Issue update occurred.
