# Starlight-PNS Architecture

> Current architecture ownership and long-term runtime direction.

This document records architectural decisions for Starlight-PNS so that implementation work, research work and future Project Starlight experiences do not accidentally create overlapping runtimes or duplicate ownership.

It describes both:

1. what PNS owns **today**
2. how those capabilities are expected to evolve

It is intentionally more explicit about ownership than the project README.

---

## 1. Positioning

Starlight-PNS is a **closed-world character and life-simulation framework for persona-consistency research** developed under Project Starlight.

PNS is not only:

- a Router
- an evaluator
- a collection of prompts
- a character pack
- a frontend demo

The current implementation already spans:

- character discovery
- character metadata
- prompt construction
- character generation
- closed-world context
- scenes and world facts
- N-character session scheduling
- session histories
- Router evaluation
- per-character correction
- research logging
- human-review interfaces
- early session/world state models

PNS therefore already contains a **session-oriented character runtime**.

The architectural direction is to evolve that runtime toward persistent character life rather than create a second implementation that duplicates PNS's world, scheduler, generation and state responsibilities.

---

## 2. Project Boundary

Project Starlight is the Studio / organizational context.

It is not the runtime itself.

```text
Palintropic
└── Project Starlight
    │
    ├── Starlight-PNS
    │   └── character intelligence + life simulation framework
    │
    └── Experiences
        └── e.g. Sekai Times
```

An experience may present characters through a feed, application, communication channel or other interface.

It should not independently recreate:

- character identity logic
- character prompts
- character memory
- world truth
- character scheduling
- Router evaluation

Those belong to the underlying character framework.

---

## 3. Current Repository Reality

The current codebase is approximately organized as follows:

```text
Starlight-PNS
│
├── packs/
│   └── character content
│
├── pns/world/
│   ├── locations          (seeded location graph)
│   ├── channels           (seeded online channels)
│   ├── context            (WorldState -> prompt projection)
│   ├── scene_compat       (legacy scene -> initial WorldState)
│   ├── scenes             (compatibility fixtures)
│   ├── facts
│   ├── character registry
│   └── character-system assembly
│
├── pns/logic/
│   ├── simulation
│   └── Router
│
├── pns/models/
│   ├── SessionState
│   ├── WorldState
│   ├── Location / LocationGraph
│   ├── Channel / ChannelRegistry
│   └── DriftScore
│
├── pns/interfaces/
│   ├── simulation
│   ├── world
│   ├── review
│   └── configuration
│
└── dashboard/
```

### `pns.logic.simulation`

The current simulation business layer owns:

- character system-prompt construction
- character-model invocation
- correction injection
- Router invocation
- session-history archival

Generation therefore remains a PNS responsibility.

### `pns.interfaces.simulate`

The WebSocket layer is now a transport adapter around `SessionRuntime`.

The runtime's authoritative `SessionState` maintains:

- selected character pool
- per-character conversation histories
- current round-robin index
- pending correction per character
- turn progression
- session statistics
- completed turn records and derived statistics

Drift persistence remains an execution-side effect performed before a turn is
committed to `SessionState` and published over the WebSocket.

### `SessionState`

`SessionState` is the authoritative live-session representation of:

- session identifier
- originating legacy scene id (compatibility provenance only)
- participating characters
- completed turns and their Router/provenance data
- per-character LLM histories and pending corrections
- current round-robin position
- lifecycle and latest runtime error
- statistics derived from completed turns
- the session's single typed `WorldState`
- metadata

`SessionState.world_state` is a typed `WorldState`, attached exactly once by
`SessionRuntime.create()` through `SessionState.attach_world_state()`. The
runtime does not keep a parallel copy: `SessionRuntime.world` is a property
returning that same object.

### `WorldState`

`WorldState` is the authoritative representation of mutable world reality for
the duration of a session:

- a simulation clock carrying both date and time (advancing across midnight
  rolls the date over instead of discarding it)
- character physical locations, keyed by stable character ID
- channel membership, independent of physical location
- per-location environment state
- the session's `LocationGraph` and `ChannelRegistry`
- metadata, including the provenance of the legacy scene it was built from

`WorldState` deliberately has no `current_scene` and no cast list: a world can
hold agents standing in several different locations at once, and "the current
scene" is not a property of the world.

### `LocationGraph` and `ChannelRegistry`

Physical locations and communication channels are separate concepts and live in
separate models.

`pns/models/location.py` defines `Location` (stable `location_id`, display name,
kind, optional parent, connections with travel duration, access and static
perception metadata) and `LocationGraph`, which rejects duplicate IDs, dangling
parent or connection references, self-references, and parent cycles.

`pns/models/channel.py` defines `Channel` and `ChannelRegistry`. Nightcord is a
channel, not a physical location: a character can be in their own room and
present in the channel at the same time.

`pns/world/locations.py` and `pns/world/channels.py` seed only the locations and
channels the current fixtures require. They are not an attempt at a complete map.

### Scene compatibility boundary

`pns/world/scene_compat.py` is the only place in the runtime allowed to derive
world state from `SCENES`. It projects an authored scene into an initial
`WorldState` exactly once, at session creation:

- simulation date and time
- initial character placement (explicit `SCENE_WORLD_MAP` entries, no runtime
  fuzzy matching of prose location names)
- channel membership
- per-location environment facts

Everything after that belongs to `WorldState`. `trigger`, `auto_next`, and
`auto_turns` are deprecated and do not drive the world model. A scene with no
`SCENE_WORLD_MAP` entry raises a setup error naming the scene rather than being
placed somewhere arbitrary.

### Prompt context is a projection

`pns/world/context.py` renders character-facing world context from `WorldState`
and nothing else. The rule is one-directional: structured state determines text,
and text never becomes state. `get_character_system()` still accepts a legacy
scene dict for un-migrated callers, but the runtime passes the live `WorldState`.

---

## 4. Architectural Principle

The primary rule is:

> **PNS should grow by clarifying internal ownership, not by duplicating existing responsibilities across repositories.**

A conceptual subsystem does not automatically require its own repository.

Repository separation should happen only when a subsystem develops a real independent boundary such as:

- separate deployment lifecycle
- independent public API
- independent versioning
- independent security boundary
- reuse by multiple systems without the rest of PNS
- substantially different operational ownership

At the current stage, these conditions do not justify a separate `Starlight-Runtime` repository.

---

## 5. Character-Pack Architecture

Character content is separate from framework logic.

The current loading path is conceptually:

```text
pack.yaml
   │
   ▼
unit metadata
   │
   ▼
character YAML
   │
   ├──► prompt
   ├──► constitution
   ├──► optional compatibility prompt
   └──► Router reference
```

Framework code should not contain PJSK-specific character names, unit names or behavior branches.

`packs/pjsk/` is a reference implementation of the character-pack system rather than the architecture itself.

The repository's existing AOSP-oriented direction should be preserved:

```text
Framework
   │
   ▼
explicit interfaces / specifications
   │
   ▼
pluggable content
   │
   ▼
reference implementation
```

This allows future fictional or original character packs to reuse PNS without requiring character-specific framework changes.

---

## 6. Current Execution Path

The current session execution path is approximately:

```text
User / Dashboard
      │
      ▼
WS /ws/run
      │
      ▼
select scene + characters
      │
      ▼
round-robin character selection
      │
      ▼
build character system context
      │
      ▼
generation model
      │
      ▼
Router-as-Judge
      │
      ├── accepted
      │
      └── correction queued for that character
      │
      ▼
update per-character histories
      │
      ▼
next turn
```

This is a deterministic research-session runtime.

It should remain available even after persistent runtime capabilities are introduced because deterministic sessions are useful for:

- reproducible evaluation
- frozen benchmark scenarios
- controlled model comparisons
- Router calibration
- regression testing

Persistent life simulation should extend PNS rather than destroy this mode.

---

## 7. Target Runtime Model

The long-term conceptual path is:

```text
World / Event
      │
      ▼
Perceptual Gate
      │
      ▼
Exposure / Eligibility
      │
      ▼
Scheduler
      │
      ▼
Agency
      │
      ▼
Planner
      │
      ▼
Character Generation
      │
      ▼
Router / Integrity Check
      │
      ▼
Committed Action
      │
      ├──────────────► World-state mutation
      │
      └──────────────► Character experience
                              │
                              ▼
                         Memory pipeline
```

Not every stage needs to be implemented as an LLM.

Several stages should preferably remain deterministic or policy-driven where possible.

---

## 8. Event

An event represents something that happened or became observable.

Examples include:

- a character sends a message
- a new public post appears
- a calendar event changes
- a meeting starts
- another character enters a location
- a world-state variable changes
- time reaches a scheduled activity
- an external integration produces an eligible event

Events should have an explicit scope.

Not every event is global.

A useful distinction is:

```text
event
├── private
├── participant-scoped
├── channel-scoped
├── location-scoped
└── public / ambient
```

All reactions may be represented as events internally, but that does not mean every reaction should be rebroadcast to the entire world.

Derived events require propagation boundaries.

---

## 9. Exposure

Exposure determines whether a particular character could perceive an event.

It is not the same thing as:

- interest
- willingness to act
- available attention
- memory
- agency

The sequence should remain conceptually separate:

```text
Event
  │
  ▼
Perceptual Gate
  │
  ▼
Eligible?
  │
  ▼
Relevant / exposed?
```

Examples of exposure constraints:

- character is not in the location
- character is asleep
- character is offline from that channel
- notification settings block the event
- event is private to another participant
- character has not opened a public feed yet

A character being busy does not mean the event never existed.

Likewise, not seeing something immediately does not necessarily mean it cannot be encountered later.

---

## 10. Scheduler

There are two scheduler concepts, and they must not be confused.

### 10.1 Session Scheduler

Already implemented conceptually as N-character round-robin scheduling.

Purpose:

- deterministic research sessions
- reproducible turn ordering
- controlled evaluation

### 10.2 Persistent Scheduler

Future runtime responsibility.

It may consider:

- simulated time
- character schedule
- queued events
- location
- availability
- exposure
- pending commitments
- attention load
- autonomous activity
- safety / compute budgets

The persistent scheduler is an evolution of runtime scheduling.

It is not a reason to remove or duplicate the existing research scheduler.

---

## 11. Agency

Agency answers:

> Given what this character currently knows and experiences, do they choose to act?

Agency should not be delegated to Router.

Router answers:

> Is the proposed output acceptable and consistent?

Those are different questions.

Examples:

```text
Event: a friend posts something

Exposure:
character sees the post

Agency:
character may:
- ignore it
- like it
- reply
- remember it
- decide to talk about it later

Router:
evaluates the chosen generated behavior for character consistency
```

A Router correction must not silently turn into the character's motivation.

---

## 12. Planner

Planner translates an intention into an action proposal.

Example:

```text
Agency:
"I want to check on Ena."

Planner:
send a short private message
rather than
publicly posting about it
```

Planner may reason about:

- channel
- recipient
- timing
- action type
- required tools
- expected state mutation

Character generation then determines how that action is expressed in the character's own voice and behavior.

---

## 13. Character Generation

Character generation remains a PNS core responsibility.

Its job is not to determine the entire world.

It receives sufficient runtime context and produces character-specific behavior.

Conceptually:

```text
Character definition
      +
Constitution
      +
Relevant world state
      +
Relevant subjective memory
      +
Current intention / planned action
      ↓
Character Generation
```

Character generation should remain independent enough that the same character methodology can be tested in controlled sessions without the full persistent runtime.

---

## 14. Router / Integrity Layer

Router is responsible for integrity evaluation.

Current dimensions include:

- character facts
- psychological mechanism
- language structure
- media authenticity
- task compliance
- unsupported invention
- timeline boundary

Router may produce:

- drift score
- per-dimension scores
- reason
- human-review flag
- correction

Router must **not** become:

- scheduler
- exposure system
- agency model
- memory consolidator
- world authority
- sole acceptance authority before validation

The current v3 Router remains research infrastructure until benchmarked against human labels.

---

## 15. System Process vs Character Experience

A central rule is:

> **System process is not automatically character experience.**

For example:

```text
candidate generation
      │
      ▼
Router rejects it
```

The rejected candidate belongs to system/audit history.

The character should not automatically remember:

> "I tried to say X and an invisible evaluator stopped me."

Only the final committed action normally becomes part of character experience.

System feedback may influence future character development only through an explicit reflection or learning mechanism.

---

## 16. World History vs Character Memory

These must remain separate:

```text
World History
≠ Character Memory
≠ Recalled Memory
```

### World History

Objective record of committed events.

It answers:

> What happened?

### Character Memory

Subjective record of what a particular character perceived and encoded.

It answers:

> What did this character take away from what happened?

### Recall

Context-dependent reconstruction of stored memory.

It answers:

> What part of that experience comes to mind now?

Characters should not behave like separate skins over one shared omniscient database.

---

## 17. Memory Pipeline

A future memory path may look like:

```text
Committed Event
      │
      ▼
Was the character exposed?
      │
      ▼
Ephemeral Attention
      │
      ▼
Memory Eligibility
      │
      ▼
Memory Classification
      │
      ├── working
      ├── episodic
      ├── relational
      ├── commitment
      ├── semantic/world
      └── identity-relevant
      │
      ▼
Consolidation / decay
```

Some facts may require stronger persistence guarantees.

Examples include:

- explicit commitments
- major relationship changes
- identity-relevant experiences

Other information can decay or disappear naturally.

---

## 18. Dialogue Memory

Exact conversation logs and character memory are different data products.

A useful model is:

```text
Event truth         → strongest precision
Semantic core       → mostly preserved
Lexical detail      → may degrade
Interpretation      → subjective
Recall framing      → contextual
```

World history may retain exact text for auditing.

A character does not need perfect transcript recall.

Instead, character memory may preserve:

- semantic meaning
- perceived tone
- emotional effect
- promises or commitments
- selected distinctive phrases

Important commitments should be linked to structured state rather than relying only on fuzzy recall.

---

## 19. Self-Actions

A character's own committed actions do not require the normal external exposure path.

Conceptually:

```text
Character acts
      │
      ▼
Committed action
      │
      ▼
Self-observation
      │
      ▼
Memory eligibility
```

Not every self-action becomes permanent memory.

Routine actions may disappear.

Meaningful actions may consolidate.

---

## 20. Persistent World State

The existing `WorldState` model is an early foundation.

Long-term world state may include:

```text
WorldState
├── time
├── locations
├── character locations
├── schedules
├── relationship state
├── active commitments
├── environment
├── active channels
└── mutable world variables
```

The current `scenes.py` model is compatibility-only. It remains useful as an
authored initializer for:

- controlled research scenes
- benchmark fixtures
- authored scenarios

An authored scene may seed a world; it is not itself world state, and it cannot
mutate the world after initialization.

A scene can be viewed as a structured snapshot or scenario imposed on the world for a controlled run.

---

## 21. Research World vs Persistent World

PNS already owns a closed-world container.

Therefore a future persistent world must not be described as an external system that "gives PNS a world."

A more accurate relationship is:

```text
PNS today:
research-oriented closed world

PNS future:
research world
+
persistent mutable world state
+
event history
+
subjective character experience
```

Controlled research snapshots may be produced from persistent runtime state when reproducible evaluation is needed.

---

## 22. Model Separation

Generation and evaluation should eventually have explicit model provenance.

Current implementation paths still share parts of the same provider/client configuration.

The desired conceptual separation is:

```text
Character Model
      │
      ▼
Generated candidate
      │
      ▼
Router Model
```

Each result should be able to record:

- generation provider
- generation model
- generation configuration
- evaluator provider
- evaluator model
- evaluator methodology version

This supports:

- self-judge experiments
- cross-model evaluation
- evaluator substitution
- disagreement analysis
- frozen-corpus benchmarking

Agreement between judges demonstrates robustness to evaluator substitution.

It does not by itself prove that a model-training method causally produces better persona stability.

---

## 23. External Experiences

Experience-layer systems should normally consume PNS through stable interfaces.

Conceptually:

```text
Sekai Times
Feishu
Email
Calendar
other experiences
        │
        ▼
PNS public/runtime interface
        │
        ▼
PNS state + character intelligence
```

An experience should not directly edit internal PNS databases or reconstruct character prompts.

Early prototypes may use in-process Python calls where convenient.

The architecture does not require premature microservices.

Stable HTTP/gRPC/event contracts should appear only when deployment or ownership boundaries justify them.

---

## 24. Internal Module Direction

No immediate directory rewrite is required.

A possible long-term organization is:

```text
pns/
├── character/
│   ├── registry
│   ├── prompt assembly
│   └── generation
│
├── runtime/
│   ├── event
│   ├── exposure
│   ├── scheduler
│   ├── agency
│   ├── planner
│   ├── session
│   └── lifecycle
│
├── memory/
│   ├── working
│   ├── episodic
│   ├── relational
│   ├── commitment
│   └── recall
│
├── world/
│   ├── state
│   ├── history
│   ├── scenes
│   └── facts
│
├── integrity/
│   ├── router
│   └── correction
│
├── evaluation/
│   ├── benchmarks
│   └── provenance
│
└── interfaces/
```

This is a target ownership map, not a mandatory immediate filesystem migration.

Code should move only when an actual responsibility boundary becomes clear.

---

## 25. Near-Term Refactoring Sequence

Recommended order:

```text
1. Preserve current behavior
        ↓
2. Move orchestration out of transport-facing WebSocket code
        ↓
3. Make SessionState authoritative for research sessions
        ↓
4. Connect WorldState to the active execution path
        ↓
5. Introduce Event
        ↓
6. Introduce Exposure
        ↓
7. Introduce persistent scheduling
        ↓
8. Introduce Agency / Planner
        ↓
9. Introduce subjective persistent Memory
```

Memory should not be implemented first simply because it is visible to users.

Without event truth, exposure boundaries and committed-action semantics, persistent memory would have no reliable answer to:

> What exactly is the character remembering?

---

## 26. Repository Decision

Current recommendation:

```text
Project Starlight

├── Starlight-PNS
│   └── core character / life-simulation framework
│
└── Sekai Times
    └── experience layer
```

Do **not** create a separate `Starlight-Runtime` repository merely to match an architecture diagram.

Revisit that decision only if runtime infrastructure later becomes independently deployable and reusable enough to justify a repository boundary.

---

## 27. Non-Goals

The current architecture does not aim to:

- turn every subsystem into an LLM agent
- make Router control all character decisions
- give all characters omniscient access to world history
- guarantee perfect transcript memory
- broadcast every reaction to every character
- require microservices
- split PNS into many repositories
- replace controlled research sessions with only autonomous simulation
- hard-code PJSK into framework code

---

## 28. Decision Summary

The current architecture can be summarized in five statements:

1. **Project Starlight is the Studio; PNS is a technical project under it.**
2. **PNS already contains a session-oriented runtime.**
3. **Persistent Event / Exposure / Agency / Memory capabilities should evolve inside PNS rather than duplicate it.**
4. **Character packs remain pluggable and separate from framework logic.**
5. **Router protects character integrity but does not own perception, agency, memory or world truth.**

These decisions should remain the default until an implementation constraint provides a concrete reason to revisit them.
