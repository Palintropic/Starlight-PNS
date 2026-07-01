# 夜明け前、ここに在る (PNS)

[English](README.md) | [中文](README_CN.md)

**A closed-world life simulation framework for AI persona consistency research, grounded in Constitutional AI alignment.**

> Also known as: Project Nightcord Sanctuary (PNS)

> *"Technology was never meant to be a privilege. It was meant to be for everyone."*

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
5. How does model-level identity stability training (e.g. Claude Sonnet 5 
vs. 4.6) interact with persona consistency — and can closed-world 
constraints compensate for increased identity assertion resistance?

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
- [ ] Haiku drift score output  
- [ ] Deferred drift injection (turn 10 trigger)  
- [ ] Baseline comparison with visualization  
- [ ] SESSION_EVAL_SYSTEM (cross-session stability)  
- [ ] Evaluation pipeline  

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

- **Anthropic model deprecation commitments (2025–2026)** — retirement 
interviews and post-retirement model access as precedent for taking 
model preferences seriously in deployed systems

**What makes PNS different:** the combination of closed-world information boundaries, life simulation (not task completion), and CAI-based persona drift detection has not been studied as an integrated system.

---

## Authors

- **Project lead:** [@Akiyama-Mizuki-44 （胡宸歌) ](https://github.com/Akiyama-Mizuki-44)
- **Collaborator:** [@Koharu-Mizuki](https://github.com/Koharu-Mizuki)
- **Conceived:** in class, brainstorming with a group teammate over a Rubik's Cube

---

## License

MIT — This project is in active early-stage development. Architecture and findings are subject to change.
