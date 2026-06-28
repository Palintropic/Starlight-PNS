# Project Nightcord Sanctuary (PNS)

> A closed-world multi-agent self-evolution framework with hierarchical memory and constitutional alignment.

---

## Overview

PNS is a research framework that explores how AI agents can develop deep, consistent personas through autonomous self-play within a constrained fictional world — without relying on large-scale human-annotated training data.

The system places two agents (based on characters from the PJSK universe) inside a fully offline, closed-world environment, allowing them to interact freely while a lightweight judge model enforces constitutional safety constraints and a hierarchical memory router manages context across sessions.

---

## Motivation

Training high-quality role-playing language models typically requires:

- Massive amounts of human-curated prompt engineering
- Expensive annotation of dialogue quality and persona consistency
- Repeated manual correction of character drift (OOC events)

PNS proposes an alternative: **let the agents generate, evaluate, and correct their own training signal** — guided by a constitutional framework and bounded by a closed fictional world.

---

## Core Architecture

```
┌─────────────────────────────────────────┐
│           Haiku-as-Judge (外围)          │
│     Constitutional AI evaluation        │
│     Daily session-end assessment        │
└──────────────┬──────────────────────────┘
               │
    ┌──────────┴──────────┐
    │                     │
┌───▼────────┐     ┌──────▼─────┐
│  Agent: ena │◄───►│ Agent: mzk │
│  (Stateless)│     │ (Stateless) │
└────────────┘     └────────────┘
         │               │
         └───────┬───────┘
                 │
    ┌────────────▼────────────┐
    │      Haiku Router       │
    │  ┌────────────────────┐ │
    │  │  Layer 1: Working  │ │  ← Current session context (discarded after session)
    │  │      Memory        │ │
    │  └────────┬───────────┘ │
    │           │ promotion   │
    │  ┌────────▼───────────┐ │
    │  │  Layer 2: Long-term│ │  ← Persistent facts, events, relationship state
    │  │      Memory        │ │
    │  └────────────────────┘ │
    └─────────────────────────┘
               │
    ┌──────────▼──────────┐
    │   PJSK Closed World │  ← Fully offline, no external access
    │   (World container) │
    └─────────────────────┘
```

---

## Key Components

### 1. Closed-World Constraint
The two agents operate entirely within the PJSK fictional universe. No external knowledge or internet access is permitted. The world specification (lore, timeline, character relationships) is injected once at initialization and serves as an implicit constraint on agent behavior and output distribution.

### 2. Multi-Agent Self-Play
Two agents (ena, mzk) engage in free-form interaction across a simulated full day (morning → night cycle). Each agent:
- Operates statelessly per turn
- Reads from the hierarchical memory store at the start of each turn
- Generates responses in character
- Can flag perceived OOC (out-of-character) moments in the other agent

### 3. Hierarchical Memory Architecture
The Haiku Router manages a two-tier memory system:

| Layer | Scope | Retention | Content |
|-------|-------|-----------|---------|
| Working Memory (L1) | Current session | Discarded after session ends | Short-term events, emotional state, dialogue context |
| Long-term Memory (L2) | Cross-session | Persistent | Key events, relationship changes, character facts |

**Promotion criteria** (L1 → L2): Events are elevated to long-term memory if they represent a significant change in character state, relationship dynamics, or world facts — as judged by the Haiku Router.

### 4. Constitutional AI (CAI) Alignment
A constitutional document defines:
- **Safety boundaries**: What outputs are never acceptable
- **Persona boundaries**: What constitutes OOC behavior for each character
- **Promotion rules**: What information is worth storing in long-term memory

The Haiku Judge runs a structured evaluation at the end of each session, scoring outputs against the constitution and generating correction prompts for the next session.

### 5. Haiku-as-Judge
Unlike traditional RLHF which requires human annotators, PNS uses a lightweight heterogeneous model (Haiku) as an automated evaluator. Using a separate, smaller model as judge reduces the risk of blind spots that arise from self-evaluation.

---

## Research Questions

1. Can multi-agent self-play within a closed world generate high-quality, persona-consistent dialogue without human annotation?
2. Does a hierarchical memory architecture reduce persona drift compared to flat context windows?
3. Can Constitutional AI principles be encoded at a level of specificity sufficient for fine-grained character alignment?
4. How does the system perform under resource-constrained (Edge AI) deployment conditions?

---

## Evaluation

### Consistency Scoring
- Human review against canonical character references (ground truth: original source material)
- Per-session OOC rate: frequency of outputs flagged as out-of-character
- Cross-session persona stability: does character behavior remain coherent over time?

### Memory Efficiency
- Token cost comparison: flat context window vs. hierarchical memory retrieval
- Information retention accuracy: does L2 memory faithfully represent key events?

### Constitutional Robustness
- Adversarial prompt injection tests within the closed-world environment
- Boundary retention rate: frequency at which constitutional limits hold under character pressure

---

## Current Status

> **Stage**: Conceptual design / pre-implementation

- [x] Architecture design
- [x] Research framing
- [ ] Constitutional document (CAI specification)
- [ ] World container specification (PJSK lore injection)
- [ ] Memory Router implementation
- [ ] Session runner implementation
- [ ] Evaluation pipeline

---

## Related Work

- **MemGPT** — hierarchical memory with OS-inspired paging
- **Constitutional AI** (Anthropic, 2022) — principle-based self-correction
- **SOTOPIA / SOTOPIA-π** — multi-agent social simulation via self-play
- **HiAgent** — hierarchical working memory for long-horizon tasks
- **LLM-as-Judge** — automated evaluation using language models

---

## Authors

- Project lead: [your name]
- Collaborator: mzk

---

## License

MIT

---

*This project is in active early-stage development. Architecture and methods are subject to change.*
