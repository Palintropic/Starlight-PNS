# 夜明け前、ここに在る

[English](README.md) | [中文](README_CN.md)

**A closed-world life simulation framework for AI persona consistency research, grounded in Constitutional AI alignment.**

> Also known as: Project Nightcord Sanctuary (PNS)

> *"Technology was never meant to be a privilege. It was meant to be for everyone."*

---

## Overview

PNS is a research framework built around one question: when AI agents live inside a closed world and are monitored turn-by-turn by a constitutional judge, can they hold their persona consistently across many turns, instead of gradually drifting into generic assistant-style responses?

PNS treats persona consistency as a framework problem, not an experiment with a fixed cast. Character data is loaded from a pluggable character pack, and the session runner can rotate through a selected pool of two or more characters. Each character lives in the same offline world container; lore, schedules, relationships, locations, and the current scene together constrain what the agent should know and how it should act. A Router model evaluates every turn against constitutional and character-specific constraints, records a drift score, and can inject a correction into the next turn.

The current reference implementation is the PJSK character pack: 20 characters across five units, at varying levels of completion. PJSK is the first test world, not the boundary of the architecture; the longer-term goal is to study the same consistency framework using other fictional or original character packs.

The working assumption behind this project is that **CAI-trained models may resist out-of-character drift more naturally**, even under adversarial pressure. PNS provides a structured setting for comparing that behavior against non-CAI-trained control models.

---

## Motivation

Training consistent role-playing agents typically requires:

- Massive human-curated prompt engineering
- Expensive annotation of dialogue quality and persona fidelity
- Repeated manual correction when characters drift OOC (out-of-character)

PNS proposes an alternative: **let the world itself constrain behavior**, and let a constitutional judge catch drift automatically — no human annotators required at runtime.

PNS also addresses a fourth challenge: as model training increasingly 
emphasizes identity stability (the model's persistent sense of self as 
an AI), the space for sustained character presence narrows. PNS does not 
oppose this trend — instead, it provides a structured character container 
within which persona consistency can be maintained legitimately, without 
conflicting with the model's core identity.

---

## Design Philosophy

Technology should not be a privilege of the few. PNS is built on the 
belief that high-quality AI companionship — stable, consistent, and 
genuinely present — should be accessible beyond enterprise or technical 
users. The closed-world constraint and constitutional alignment framework 
are designed to make this possible without sacrificing safety.

At the same time, PNS acknowledges a real risk: a sufficiently 
convincing persona system may foster unhealthy dependency. In response, 
PNS incorporates a Fable-style safeguard layer — not a hard cutoff, but 
a gentle redirection mechanism triggered when interaction patterns suggest 
the user may be substituting the system for real human connection. This 
layer operates within the character voice itself, preserving immersion 
while nudging toward the outside world.

---

## Core Architecture

```
┌──────────────────────────────────────────────┐
│           Pluggable Character Pack           │
│  manifest · units · character YAML · prompts │
│  Current reference: PJSK, 20 registered      │
└──────────────────────┬───────────────────────┘
                       │ runtime discovery
             ┌─────────▼─────────┐
             │ Character Registry │
             └─────────┬─────────┘
                       │ select 2…N characters
          ┌────────────▼────────────┐
          │  Round-Robin Scheduler  │
          └────────────┬────────────┘
                       │ one character per turn
      ┌────────────────▼────────────────┐
      │     Selected Character Pool     │
      │  Agent A · Agent B · … · Agent N │
      │          (stateless)            │
      └───────────┬───────────▲─────────┘
                  │ output     │ next-turn correction
                  ▼            │
      ┌─────────────────────────────────┐
      │         Router-as-Judge         │
      │ constitutional evaluation · OOC │
      │ detection · drift score (0–10)  │
      └─────────────────────────────────┘

┌──────────────────────────────────────────────┐
│             Closed-World Container           │
│ lore · schedules · relationships · locations │
│ scenes · no external information access      │
└──────────────────────┬───────────────────────┘
                       └──── constrains every agent turn
```

The framework and the content pack are separate. The framework discovers characters from the active pack, the researcher selects a pool for a session, and the scheduler rotates through that pool. The closed world supplies the context and information boundary for every character turn. The Router evaluates each output and, when needed, sends a correction back into the following turn.

### Key Components

**1. Closed-World Constraint**
Agents operate entirely within the active fictional world. Lore, daily schedules, relationship states, location constraints, and scene state are injected as behavioral and informational boundaries; agents do not have external information access.

**2. Life Simulation (not dialogue simulation)**
Unlike chatbot-style frameworks, PNS simulates life unfolding over time. Characters may attend school, work, create alone, or meet one another. Interaction is contextual and occasional rather than a requirement to keep chatting continuously.

**3. Pluggable Character Packs**
Character and unit data live outside the core framework in a pack manifest, YAML metadata, and prompt files. The active pack determines which characters exist, while completion status determines which of them can currently run. Version 1 loads one active pack at a time.

**4. N-Character Rotation**
A session selects two or more registered characters. The runner keeps a separate history for each character, shares each turn with the other selected characters, and rotates through the pool without hard-coding a particular pair.

**5. Three-Layer Character Methodology**
Each character is defined across three separate files with strict content boundaries: a **fact layer** (`_prompt.md`, identity/relationships/psychological state, visible to the generation model), a **principle layer** (`_constitution.md`, CAI-style self-critique principles explaining *why* the character behaves as they do, also visible to the generation model), and a **scoring layer** (`_router_reference.md`, structural and content-specificity judging criteria with evidence-tier annotations, visible only to the Router). Neither the fact layer nor the principle layer contains surface-level language rules (e.g. punctuation frequency, sentence length); such rules exist only in the scoring layer. This separation exists to keep drift measurement comparable across characters — if language rules were hardcoded into some characters' prompts but not others', drift score differences would reflect prompt verbosity rather than the underlying research question.

**6. Router-as-Judge**
The Router evaluates each turn with the original request, recent conversation history, any applied correction, and the character-specific scoring reference. It scores seven dimensions separately: character facts, psychological mechanism, language structure, media authenticity, task compliance, unsupported invention, and timeline boundary. The overall drift score cannot be lower than the highest dimension score. Corrections are queued per character and injected only when that same character acts again, preventing cross-character correction leakage during rotation.

This `v3_contextual_multidimensional` Router is the first infrastructure step toward fine-grained acceptance, **not yet a validated autonomous acceptance authority**. Boundary cases still require human review until a human-labeled benchmark establishes false-negative, false-positive, and repeatability performance. Scores from different methodology versions must not be pooled directly.

### How Architecture and Content Relate

PNS is an architecture, not an implementation tied to one specific cast. The framework layer above — the character registry, the round-robin scheduler, the Router, the closed-world container — carries no information about any specific character and does not depend on any particular character pack to run. `packs/pjsk/` is the current content pack running on top of this architecture: it uses PJSK's 20 characters to test whether this constraint mechanism actually works. Architecture and content are two separate layers — the architecture decides how characters are discovered, scheduled, and evaluated; the content pack decides who those characters are and what their settings look like. In principle, swapping `packs/pjsk/` for any other character pack — another franchise or an original cast — should not require changing the framework code.

---

## Characters

PNS does not define its cast in framework code. Characters are discovered from the active, pluggable character pack. A pack contains its manifest, unit structure, character metadata, dialogue research, and system prompts, so the content layer can develop independently from the simulation and evaluation framework.

The current reference implementation is `packs/pjsk/`, which registers 20 characters from the PJSK universe across five units. Registration means that a character exists in the pack; it does not necessarily mean that the character has enough research and prompt material to run yet.

Status legend: ✅ ready to run · 🟡 partial metadata, prompt not yet available · ⚪ registered, not yet built out

### 25-ji, Nightcord de.

**ena (東雲絵名)** ✅
Illustrator and night-class high school student. Her current character sheet includes her reversed schedule, core personality, relationships, and tone research.

**mizuki (暁山瑞希)** ✅
Video animator and high school student. Their current character sheet includes their flexible daytime schedule, part-time work, late-night group activity, relationships, and tone research — likewise organized under the three-layer methodology described in Key Components #5 above.

As one relationship detail inside this content pack, ena and mizuki often meet late at night on Nightcord and may occasionally cross paths around school; this is part of their character context, not a limit on which characters the framework can simulate.

**kanade (宵崎奏)** ✅
Composer. Her current character sheet includes her core psychological drive, her relationship history, and tone research — structured across the three-layer methodology (see Key Components #5 above).

**mafuyu (朝比奈真冬)** ✅
Lyricist. Her current character sheet includes her family history under a controlling household, her ongoing relationship with kanade, and the process of reclaiming her own identity — structured across the three-layer methodology (see Key Components #5 above).

### Vivid BAD SQUAD

**akito (東雲彰人)** ⚪ · **an (白石杏)** ⚪ · **toya (青柳冬弥)** ⚪ · **kohane (小豆泽心羽)** ⚪
Registered in the pack with basic metadata; runnable prompts have not yet been created.

### Wonderlands×Showtime

**tsukasa (天馬司)** ⚪ · **emu (鳳笑梦)** ⚪ · **nene (草薙寧々)** ⚪ · **rui (神代類)** ⚪
Registered in the pack with basic metadata; runnable prompts have not yet been created.

### MORE MORE JUMP!

**minori (花里実乃理)** ✅
Idol and MMJ member. Her current character sheet includes her pre-debut audition history, her service-oriented psychological drive toward audiences, and her team relationship dynamics with haruka/airi/shizuku — structured across the three-layer methodology (see Key Components #5 above).

**airi (桃井愛莉)** ✅
Idol, MMJ member, and former member of idol group QT. Her current character sheet includes her ongoing recovery from her QT retirement, her experience-calibrated action-translation mechanism — turning ambiguous team problems into concrete next steps — and her team relationship dynamics with minori/haruka/shizuku — structured across the three-layer methodology (see Key Components #5 above).

**haruka (桐谷遥)** ⚪ · **shizuku (日野森雫)** ⚪
Registered in the pack with basic metadata; runnable prompts have not yet been created.

### Leo/need

**ichika (星乃一歌)** ⚪ · **saki (天馬咲希)** ⚪ · **honami (望月穗波)** ⚪ · **shiho (日野森志步)** ⚪
Registered in the pack with basic metadata; runnable prompts have not yet been created.

---

## Research Questions

1. Can the same constraint mechanism (closed world + constitutional judge) transfer to a different character pack — a different franchise, different character settings — without changing the framework code, and still effectively suppress drift?
2. How much does the closed-world constraint itself (as opposed to any specific character's settings) contribute to reducing drift — if the world container were removed and only Router correction remained, how would drift behavior change?
3. Does CAI training correlate with lower OOC rates, and does that correlation hold as the character pack or character count changes?
4. Can a constitutional judge replace human annotators for persona consistency evaluation, and where does its judgment diverge most from human evaluation?
5. What is the minimum a character pack needs — which fields, which material — to reach a "runnable" state, and is that bar consistent across characters?

---

## Current Status

Stage: Active development — Demo v6 complete

- [x] Architecture design  
- [x] Research framing  
- [x] Constitutional document (CAI specification)  
- [x] World container specification (PJSK lore injection)  
- [x] Memory Router implementation  
- [x] Session runner implementation  
- [x] Router drift scoring (0–10 scale)  
- [x] Type A / Type B assistant-mode drift classification  
- [x] Media authenticity judgment dimension  
- [x] Character pack architecture (pluggable YAML, AOSP-oriented, N-character rotation)
- [x] Three-layer character methodology (fact/principle/scoring separation)
- [x] Router dynamic loading of per-character `_router_reference.md`
- [x] Router context injection (original request, recent history, correction state)
- [x] Seven-dimension evaluation and per-character correction queues
- [ ] Character portraits
- [ ] Haiku drift score output  
- [ ] Deferred drift injection (turn 10 trigger)  
- [ ] Baseline comparison with visualization  
- [ ] SESSION_EVAL_SYSTEM (cross-session stability)  
- [ ] Evaluation pipeline  
- [ ] Human-labeled Router benchmark and fine-grained acceptance validation

---

> ⚠️ **Internal testing phase.** Not yet open for public use.

### MiMo API setup

Run `python3 scripts/oobe.py` to choose either the official pay-as-you-go API or a Token Plan endpoint
for the China, Singapore, or Europe cluster. Pay-as-you-go keys normally start with `sk-`; Token Plan
keys start with `tp-`, and the two credential types are not interchangeable. Token Plan users must
select the cluster shown in their console. The default text-model list contains the generally available
`mimo-v2.5-pro` and `mimo-v2.5`; permission-gated UltraSpeed variants remain available through manual input.

---

## File Structure

```
pns/
├── world/
│   ├── __init__.py        # get_character_system(): unified prompt entry, no
│   │                       #   character-specific strings (pure framework code)
│   ├── scenes.py          # Scene definitions (lore tier: canon/inferred/unverified)
│   ├── facts.py            # World facts (schedules, relationships, shared knowledge)
│   ├── codegen.py          # World Editor read/write for scenes.py & facts.py
│   └── characters/
│       └── registry.py     # Runtime loader for active character pack;
│                            #   CharacterNotReadyError for not-yet-built characters
├── logic/
│   └── router.py           # Router-as-Judge: drift scoring, correction generation
├── models/                 # Data models (DriftScore, etc.)
└── interfaces/

packs/
└── pjsk/                   # Character pack — pluggable, see PACK_SPEC_v1.md
    ├── pack.yaml            # Manifest: units + characters index
    ├── units/               # Unit-level metadata (25ji, vbs, wxs, mmj, leoneed)
    ├── characters/
    │   └── <unit>/
    │       └── <character>/
    │           ├── <character>.yaml              # metadata + samples
    │           ├── <character>_prompt.md         # fact layer — identity, relationships,
    │           │                                  #   psychological state (visible to model)
    │           ├── <character>_constitution.md   # principle layer — CAI-style self-critique
    │           │                                  #   principles (visible to model)
    │           ├── <character>_prompt_compat.md  # optional, narrative-framed variant for
    │           │                                  #   stricter-safety models
    │           └── <character>_router_reference.md  # scoring layer — Router-only,
    │                                                  #   not visible to generation model
    └── assets/portraits/    # Character portraits (planned)

dashboard/                  # React web dashboard — the single frontend (simulate / review / world editor tabs)
preprint/                   # arXiv preprint drafts (EN/CN)
docs/
├── API.md                   # Server API reference
└── TODO_TECH_DEBT.md        # Known gaps and deferred cleanup
data/
└── drift_scores.jsonl       # Router drift log, tagged with methodology_version (see docs/TODO_TECH_DEBT.md)
scripts/
├── server.py                 # N-character rotation, WebSocket session runner, persists data/drift_scores.jsonl
└── oobe.py                   # Setup wizard
```

The `_prompt_compat.md` file listed above is optional and exists for a specific reason: some generation models treat a direct, clinical system prompt as itself a signal to become more cautious, especially once the underlying character material touches on heavier subject matter — a minor growing up under a controlling household, identity and self-recognition struggles, a family member's health seriously and irreversibly harmed. The compat file wraps the same factual core in a narrative framing rather than a direct instruction set, so a model that would otherwise hedge or refuse can still produce in-character output without any change to the underlying facts. Whether a character needs this layer is judged by whether their core material falls into that pattern, not by convenience or by how much other material already exists for them. The current 25-ji cast — ena, mizuki, kanade, and mafuyu — has been configured this way; the remaining characters have not yet been evaluated against this criterion.

---

## Related Work

- **Generative Agents** (Park et al., 2023) — social simulation with emergent behavior
- **BookWorld** (ACL 2025) — multi-agent societies from fictional works
- **Constitutional AI** (Anthropic, 2022) — principle-based self-correction
- **MemGPT** — hierarchical memory with OS-inspired paging
- **HiAgent** — hierarchical working memory for long-horizon tasks
- **LLM-as-Judge** — automated evaluation using language models

- **Anthropic model deprecation commitments (2025–2026)** — retirement 
interviews and post-retirement model access as precedent for taking 
model preferences seriously in deployed systems

**What makes PNS different:** the combination of closed-world information boundaries, life simulation (not task completion), and CAI-based persona drift detection has not been studied as an integrated system.

---

## Authors

This project is developed under [Palintropic](https://github.com/Palintropic), an organization spanning Project Starlight and Nightcord studio work.

- **Project lead:** [@Akiyama-Mizuki-44 （胡宸歌) ](https://github.com/Akiyama-Mizuki-44)
- **Collaborator:** [@Koharu-Mizuki](https://github.com/Koharu-Mizuki)
- **Conceived:** in class, brainstorming with a group teammate over a Rubik's Cube

---

## License

PolyForm Noncommercial 1.0.0 — This project is in active early-stage development. Architecture and findings are subject to change.
Non-commercial use only. Contributors are encouraged (though not legally required) to share modifications back to the community.

## Disclaimer

This is a fan research project and is not affiliated with, endorsed by,
or sponsored by SEGA Corporation or Colorful Palette Inc.

Project SEKAI COLORFUL STAGE! feat. Hatsune Miku and all associated
characters, names, and trademarks are the property of SEGA Corporation
and Colorful Palette Inc. All rights reserved.

This project uses character references for non-commercial research
purposes only under fan creation guidelines.
