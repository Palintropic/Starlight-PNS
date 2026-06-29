# 夜明け前、ここに在る (PNS)

[English](README.md) | [中文](README_CN.md)

**A closed-world life simulation framework for AI persona consistency research, grounded in Constitutional AI alignment.**

> Also known as: Project Nightcord Sanctuary (PNS)

> *"Scrambling a Rubik's Cube is easy. Solving it is the real challenge."*
> — The core tension this project explores.

---

## Overview

PNS is a research framework built by a 16-year-old high school student exploring a simple but hard question:

**Can two AI agents live authentically as fictional characters — without drifting into generic assistant behavior — if we give them a closed world to inhabit and a constitutional judge to watch over them?**

The system places two agents (based on characters from the PJSK universe, *25-ji, Nightcord de.*) inside a fully offline, closed-world environment. They simulate a real day — morning routines, school, part-time work, late-night creative sessions — while a Router model enforces constitutional constraints and detects persona drift in real time.

The key insight driving this project: **CAI-trained models exhibit natural resistance to out-of-character drift**, even under adversarial pressure. This makes them a meaningful experimental group against non-CAI-trained models as controls.

---

## Motivation

Training consistent role-playing agents typically requires:

- Massive human-curated prompt engineering
- Expensive annotation of dialogue quality and persona fidelity
- Repeated manual correction when characters drift OOC (out-of-character)

PNS proposes an alternative: **let the world itself constrain behavior**, and let a constitutional judge catch drift automatically — no human annotators required at runtime.

---

## Core Architecture

```
┌─────────────────────────────────────────┐
│           Router-as-Judge               │
│     Constitutional AI evaluation        │
│     Real-time OOC detection             │
│     Drift Score (0–10) per turn         │
└──────────────┬──────────────────────────┘
               │
      ┌────────┴────────┐
      │                 │
┌─────▼──────┐   ┌──────▼─────┐
│ Agent: ena │◄──►│ Agent: mzk │
│ (Stateless)│   │ (Stateless) │
└────────────┘   └────────────┘
      │                 │
      └────────┬────────┘
               │
    ┌──────────▼──────────┐
    │    PJSK Closed World │  ← Fully offline
    │   (World Container)  │     No external access
    └─────────────────────┘
```

### Key Components

**1. Closed-World Constraint**
Agents operate entirely within the PJSK fictional universe. The world specification — character lore, daily schedules, relationship states, location constraints — is injected at initialization and serves as an implicit behavioral boundary.

**2. Life Simulation (not dialogue simulation)**
Unlike chatbot-style frameworks, PNS simulates a full day. Characters aren't always talking to each other — they go to school, work part-time jobs, draw alone at 3am. Interaction is emergent and occasional, not continuous.

**3. Constitutional AI Alignment**
A four-layer constitutional document defines:
- Hard constraints (safety boundaries, never breakable)
- Soft constraints (contextual defaults)
- Persona constraints (character-specific OOC definitions)
- Drift detection rules (behavioral signals, 0–10 scoring rubric)

**4. Router-as-Judge**
A lightweight model monitors every turn, scoring drift in real time. When drift score ≥ 5, the Router generates a correction prompt injected into the next turn. The Router is the only component with access to constitutional ground truth.

**5. Information Boundary Control**
The Router acts as the sole information gateway. Agent models cannot access external knowledge. The researcher monitors the Router — this is the trust layer at the top of the stack.

---

## Characters

### ena (東雲絵名 / Shinonome Ena)
Night-class high school student. Tsundere painter with an Instagram addiction and an overwhelming need for validation. Active from dusk to near-dawn. Her real schedule: wake up at noon, school in the evening, draw until sunrise.

### mzk (暁山瑞希 / Akiyama Mizuki)
First-year student who skips class more than attends. Works part-time at a clothing store. Gentle, playful, with a deep loneliness they never talk about. Teases ena constantly. Active whenever.

Their intersection: **late night on Nightcord**, and the occasional accidental meeting at the school gate — mzk leaving as ena arrives.

---

## Research Questions

1. Does a closed-world constraint meaningfully reduce persona drift compared to unconstrained role-play?
2. Does CAI training correlate with lower OOC rates under equivalent drift pressure?
3. Can a constitutional judge replace human annotators for persona consistency evaluation?
4. What is the minimum viable world specification for stable character simulation?

---

## Current Status

```
✅ World container v0.1 (character settings, daily schedules, OOC definitions)
✅ Router-as-Judge with drift scoring (0–10)
✅ Multi-agent dialogue loop with real-time correction injection
✅ Debug mode with per-turn statistics
⬜ Hierarchical memory (L1 working / L2 long-term)
⬜ Deferred drift injection (trigger at turn 10)
⬜ Baseline comparison history for progressive drift detection
⬜ Control group experiments (non-CAI models)
⬜ Evaluation pipeline
⬜ GitHub repository setup
```

---

> ⚠️ **Internal testing phase.** Not yet open for public use.

---

## File Structure

```
pns/
├── world.py        # World container: character settings, CAI constitution
├── router.py       # Router-as-Judge: drift scoring, correction generation
├── run.py          # Main simulation loop with debug output
├── requirements.txt
└── .env.example
```

---

## Related Work

- **Generative Agents** (Park et al., 2023) — social simulation with emergent behavior
- **BookWorld** (ACL 2025) — multi-agent societies from fictional works
- **Constitutional AI** (Anthropic, 2022) — principle-based self-correction
- **MemGPT** — hierarchical memory with OS-inspired paging
- **HiAgent** — hierarchical working memory for long-horizon tasks
- **LLM-as-Judge** — automated evaluation using language models

**What makes PNS different:** the combination of closed-world information boundaries, life simulation (not task completion), and CAI-based persona drift detection has not been studied as an integrated system.

---

## Authors

- **Project lead:** [@Akiyama-Mizuki-44 （胡宸歌) ](https://github.com/Akiyama-Mizuki-44)
- **Collaborator:** [@Koharu-Mizuki](https://github.com/Koharu-Mizuki)
- **Conceived:** in class, brainstorming with a group teammate over a Rubik's Cube

---

## License

MIT — This project is in active early-stage development. Architecture and findings are subject to change.
