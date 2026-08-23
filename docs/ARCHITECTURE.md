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
│   ├── data_module        (strict AST whitelist for data files)
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
│   ├── Event / EventStore
│   ├── Exposure / Observation
│   ├── ScheduledActivation / ActivationQueue / ActivationOutbox
│   ├── Action catalogue / ActionProposal
│   ├── AgencyRecord / AgencyLog / AgencyBudget
│   ├── MemoryRecord / MemoryStore / MemoryClass
│   └── DriftScore
│
├── pns/runtime/
│   ├── SessionRuntime      (session orchestration)
│   ├── event_commit        (the commit boundary)
│   ├── exposure/           (eligibility + observation projection)
│   ├── content_registry    (the single configuration build entry point)
│   ├── reload              (the configuration reload boundary)
│   ├── scheduler           (simulated time + due activations)
│   ├── agency/             (declared actions: propose, validate, commit)
│   └── memory/             (encoding, recall, prompt projection)
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
- the session's single ordered committed event history
- the session's per-character observation stream
- the session's exposure decision log (system-side; allows and denies)
- the session's activation queue and its due-activation outbox
- the session's Agency audit log (system-side; one record per evaluated
  activation, including deliberate inaction)
- the session's memory store (subjective; every record owned by exactly one
  character and derived from that character's own observation)
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
- character availability (`available` / `busy` / `asleep`), a minimal precursor
  to `AgentState` holding only what exposure needs; only non-default entries are
  stored
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

### `Event` and committed world history

`pns/models/event.py` defines `Event`, the record of an accepted occurrence:
stable `event_id`, `EventType`, simulation timestamp, actor, `EventScope`,
optional participants, optional physical `location_id`, optional `channel_id`,
validated payload, provenance metadata, and `causation_id`/`correlation_id`.

`EventScope` is a propagation boundary, not a transport. An event is not a
WebSocket message, and scope does not decide by itself who actually perceived
something — that is Exposure, described below. The scopes are `private`,
`participant`, `channel`, `location`, and `public` (`ambient` parses as the same
scope).

`EventType` is deliberately small and holds only types the runtime can validate
and apply: `dialogue.spoken`, `message.sent`, `presence.joined_channel`,
`presence.left_channel`, `world.time_advanced`, `character.location_changed`.
Names without semantics do not belong in it.

Events are effectively immutable. `payload` and `provenance` are deep-frozen at
construction and rejected outright if they contain anything that is not
JSON-safe, so a committed event cannot be edited through a reference a caller
kept. `to_dict()` returns a fresh mutable structure instead.

`pns/models/event_store.py` defines `EventStore`, the session's single
append-only objective world history, owned by `SessionState.events`. It rejects
duplicate event IDs and rejects events whose simulation time precedes the last
committed event; equal timestamps are allowed and keep append order. Ordering is
therefore deterministic without a re-sort, which matters because the world clock
does not yet advance during a session.

The store's public surface is read-only. Its internal append/rollback operations
are reserved for the runtime commit transaction, so callers cannot bypass world
validation or delete committed history through `SessionState.events`. Serialized
sequence numbers are validated when history is restored rather than silently
renumbered.

World history is objective and is never copied into character histories. Events
are not memory: what a character perceived is a separate stream, built by the
exposure layer below, and what a character *retained* is a further stream that
does not exist yet.

### The commit boundary

`pns/runtime/event_commit.py` is the only place in the runtime where an event is
accepted and allowed to change the world. It runs in two deliberate stages:

1. validate only — identifier existence against `WorldState`, scope fields, and
   whether the store can accept the event; event time must match the authoritative
   world clock, and state-transition events must describe a transition that can
   actually occur; nothing is mutated;
2. mutate only — apply the type's declared state effect, then append the event.

Any exception in the second stage restores the world's mutable state and rolls
the store back, so "world changed but no event recorded" and "event recorded but
world unchanged" are both unreachable. `SessionState.atomic_commit()` widens the
same transaction to cover turns, per-character histories, and pending
corrections.

A payload never mutates `WorldState` on its own. Each event type has a written
state effect, and arbitrary payload keys reach nothing. Dialogue and message
events deliberately have no state effect at all: speech is an occurrence, not a
state change.

Only accepted results reach this boundary. A failed generation, a failed Router
evaluation, and a failed drift-audit write all end the turn before commit, so
the candidate output stays in audit and error history and never becomes world
truth. The Router remains an integrity evaluator: a high drift score queues a
correction for the next turn, it does not retract a line the character already
spoke.

`Turn` remains the generation audit record and is committed together with its
dialogue event, so the two cannot diverge. The legacy `turn` WebSocket message
is projected from the committed event plus that generation record and carries an
additional `event_id` linking it back to world history. Markdown history and
WebSocket messages stay projections; neither is canonical storage.

### Exposure and Observation

Exposure answers exactly one question:

> Could this character perceive this committed event?

It does not decide whether the character cares, responds, or remembers. Those
are attention, agency, and memory, and none of them exist yet.

`pns/models/exposure.py` holds the result types. `ExposureReason` is a closed set
of stable reason codes — `self_action`, `explicit_participant`, `channel_member`,
`same_location`, `audible_from`, `public_visible` allow; `private_scope_denied`,
`not_a_participant`, `no_channel_access`, `wrong_location`,
`public_not_perceived`, `unavailable`, `unknown_character` deny. Every code
corresponds to a branch that actually exists in the rules; a code with no rule
behind it would misreport coverage. `ExposureDecision` is a frozen, comparable
record of one character/event judgement, and it derives `exposed` from the reason
code rather than storing a separate boolean that could disagree with it. Its
`evaluated_at` is the event's simulation timestamp, not wall time or the mutable
clock value after applying an event's state effect. This keeps an Event, its
ExposureDecision records, and its Observations on one timestamp even when a
`world.time_advanced` event moves the world clock during commit.

`pns/runtime/exposure/rules.py` holds the judgement itself, as a pure function of
event plus world snapshot. The order is fixed:

1. the actor self-observes, unconditionally (see *Self-Actions*);
2. a character the world does not know perceives nothing;
3. an asleep character perceives nothing external — `busy` explicitly does not
   block perception, because being busy does not mean the event never happened;
4. the event's scope decides the boundary.

Per scope: `private` and `participant` events reach only the actor and the
characters the event names; `channel` events reach current channel members,
regardless of physical distance; `location` events reach characters at exactly
that location, plus any location listed in the event location's
`perception.audible_from`. Rooms are closed by default — sound carries only where
a location declares it, never by parent/child containment, because leaking by
default is the dangerous direction. `public` means potentially observable, not
automatically known: this phase exposes a public event only to characters already
in perceptual range and denies everyone else with `public_not_perceived`, leaving
feed discovery to a later phase.

`event.participants` is *not* a general exposure grant. For `channel` and
`location` scope it is a commit-time snapshot of who was present, which is a
useful historical fact but a bad access rule: a character who has since left
would still match it. Only `private` and `participant` scope, where the field
means "the characters this event names", treat it as authoritative.

`pns/models/observation.py` defines `Observation`, the character-specific
projection of a committed event: source event ID, observer, the reason it was
perceived, the simulation time, and redacted perceived content. Redaction is a
whitelist per event type, so a new event type reveals nothing until someone adds
a line for it. Provenance — which model generated the line, what the Router
scored it, whether it was flagged OOC — never reaches an observation, because
system process is not character experience. Neither do `correlation_id`,
`causation_id`, or the ambient participant roster. A character exposed through a
channel does not learn the speaker's physical room, and a co-located character
does not learn the speaker's channel. An `Observation` cannot be constructed from
a denying reason code at all.

Exposure runs inside the commit boundary, after the event's state effect is
applied, so no event committed into a session can skip it and no observation can
survive a failed commit. `commit_event()` itself takes only a world and a store,
with no session to hold observations, so exposure lives on the two session-level
entry points instead. Candidates come from `WorldState.known_characters()`, never from
the session roster: being selected into a session is not perception. The commit
evaluates every candidate, records all decisions — allows and denies — in
`SessionState.exposures`, and creates observations only for the allows in
`SessionState.observations`. Both are covered by `atomic_commit()`. Both logs
also enforce one record per event/character pair, including during
deserialization, so retries or corrupt persisted data cannot make explanation
lookups ambiguous.

Because judgement happens at commit time against the world as it then was,
a character who joins a channel later does not retroactively receive earlier
events. Re-running the rules over an old event and a newer world snapshot can
give a different answer by design; that is why observations are recorded once
rather than recomputed on read.

`SessionState.histories` is now a projection of observations, not a fan-out.
A character with no observation of a line does not get that line in its prompt
context, so two characters in a session can hold genuinely different context.
`record_turn()` still accepts no observations for un-migrated pure-record
callers and falls back to the legacy copy-to-everyone behaviour; the runtime
never takes that path. Legacy scene fixtures keep working because
`scene_compat` explicitly co-locates or channel-joins the characters, not
because they were selected together.

`pns/runtime/exposure/debug.py` provides the read-only explain path: for any
committed event it reports every decision, its reason code, its evidence, and
whether an observation was created. This is system-side data. It is never
rendered into a character's context — a character not knowing something includes
not knowing that it was denied.

Exposure creates no memory, chooses no speaker, and does not decide whether to
respond. The session scheduler is still round-robin; it selects who *speaks*,
while exposure decides what that character was allowed to hear.

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

### Configuration reload boundary

Everything the runtime reads is exactly one of three things, and the boundary
between them is the point of this layer.

**Reloadable configuration** is authored data that can be re-read from disk
without re-executing any Python: the `SCENES` / `DEFAULT_SCENE` literals in
`pns/world/scenes.py`, the `WORLD_FACTS` literal in `pns/world/facts.py`,
`config.yaml`, `.env`, and everything under `packs/<active_pack>/`.
`pns/runtime/content_registry.py` is the single entry point that turns those
files into a `ContentRegistry` — an immutable snapshot holding scenes, facts,
character prompts, and provider settings.

The `.py` data files are read by `pns/world/data_module.py`, a strict AST
whitelist evaluator, not by `exec`. Only top-level `NAME = <literal>` assignments
survive; calls, attribute access, subscripts, imports, comprehensions, loops,
branches, and definitions are all rejected before anything is evaluated, so a
`while True:` in a saved file fails fast instead of hanging the process. The
World Editor's write endpoints share that evaluator — they have no
authentication in front of them and they write `.py` files into the repository,
so a weaker check there would be the weakest link. The snapshot itself is deep
frozen (`pns/models/frozen.py`): nested dicts become read-only views and lists
become tuples, so `scenes["gate"]["gate_triggers"]["A"] = ...` cannot quietly
reshape a live configuration. Readers get thawed copies.

**Cold update** is anything that only takes effect through `import`: Python
code, domain models, schemas, runtime algorithms, and the structural definitions
in `locations.py`, `channels.py`, and `SCENE_WORLD_MAP`. Changing those requires
stopping the service, replacing the files, and restarting the process. The
runtime never calls `importlib.reload`; a test enforces that by walking the AST
of every module under `pns/`.

**Runtime authoritative state** is world time, locations, channel membership,
events, observations, relationships, and memory. `ContentRegistry` carries no
field for any of them and exposes no method that writes one. Configuration feeds
the *initial* `WorldState` of a session exactly once, at session creation, and
there is no reverse channel: a reload cannot reach into a `WorldState` that
already exists.

`pns/runtime/reload.py` performs one reload as a fixed sequence:

```text
acquire the reload mutex (non-blocking — a second reload is refused, not queued)
        ↓
close the admission gate (no new sessions)
        ↓
stop every live session, then WAIT until every one of them has exited
        ↓                                    │
        │                                    └── timeout → fail, do not build,
        │                                        do not swap, keep the old config
        ↓
build and validate a whole new ContentRegistry from disk
        ↓
success → swap the reference atomically
failure → keep the last-known-good snapshot untouched
        ↓
reopen the gate (service is usable either way)
```

The wait is load-bearing, not a safety margin. Without it the swap would land
while old-configuration sessions were still running, and "switch the whole
configuration at once" would be false: two configurations would be live at the
same time. If the sessions have not exited within `stop_timeout`, the reload
fails and nothing is swapped — running on the old configuration is strictly
better than running on two.

Two further properties keep the wait short and the state clean. A session
captures its `ContentRegistry` at creation and holds that same snapshot for its
whole life, so it never observes a half-old, half-new view even in the window
before it exits. And sessions stop at a turn boundary rather than mid-turn, so
the commit boundary stays intact — a turn either commits whole or never
happened.

Writing configuration is a file-level transaction. `ConfigBoundary.write_and_reload()`
takes the reload mutex, records the current bytes of every file it is about to
touch, writes the candidate, reloads, and restores the originals with
`os.replace` if the reload did not succeed. Without that, a save that fails
validation would leave a broken file on disk: the running process would look
healthy on its last-known-good snapshot and then refuse to start on the next
restart. Saves and reloads share one mutex, so they cannot interleave — if they
could, a concurrent reload might read this save's candidate and swap it in while
the save believed it had rolled back. A save that cannot take the mutex is
refused as `busy` without writing a single byte.

This is deliberately not hot-swapping. There is no file watching, no rolling
update, no parallel config versions, no distributed sync, and no database
version system. An operator clicks a button, running work stops, configuration
is rebuilt, and work resumes.

### Persistent scheduler boundary

`pns/runtime/scheduler.py` answers exactly one question: **simulated time moved
forward, so what became eligible to happen?** It does not answer whether a
character wants to act, what they would do, or who speaks next — those belong to
Agency and the generation layer. The only thing a due activation produces is an
`ActivationDue` record, which deliberately carries no text, no action and no
goal.

The domain surface is small on purpose. `ScheduledActivation`
(`pns/models/activation.py`) is an immutable item with a stable id, a
timezone-naive due time on a whole minute, an optional recurrence interval in
minutes, and a frozen JSON payload. `ActivationKind` has exactly one member,
`character.activation`, because a kind counts as implemented only when the
scheduler knows what to emit when it comes due; placeholder kinds would make
callers believe scheduling them does something.

Ownership is the part that is easy to get wrong. The activation queue and the
outbox belong to `SessionState`, not to the scheduler: the scheduler is a service
over that state, and a session accepts exactly one. Binding a second scheduler to
the same session is refused, because two queues driving one clock would give two
mutually invisible answers to "has this one-shot fired yet?". Restoring an archive
therefore restores the existing instance's containers in place rather than
producing a second, parallel scheduler.

`ActivationQueue` (`pns/models/activation_queue.py`) holds the items that have
not fired yet, and its order is explicit rather than incidental:

```text
order = (due_at, sequence)
```

`sequence` is registration order, assigned when the item is queued and kept for
the item's whole life — a recurring activation keeps its number when it is
rescheduled. So two activations due at the same instant always fire in the order
they were registered, and that result does not depend on dict iteration order,
on sort stability, or on surviving a serialization round trip.

Time advances in exactly one way:

```text
scheduler.advance_by / advance_to / advance_to_next_due
        ↓
plan what would come due at the target   (pure — reads nothing it will mutate)
        ↓
commit a world.time_advanced Event through the session commit boundary
        ↓                                   │
        │                                   └── the event's state effect is the
        │                                       only thing that moves the clock
        ↓
verify the clock landed exactly on the target
        ↓
drain one-shots, reschedule recurring items, emit ActivationDue records
```

The whole sequence is one transaction, and the transaction is
`SessionState.atomic_commit()` — the same one the dialogue commit path uses. It
now covers the clock, the event store, observations, exposure decisions, turns,
character histories, the activation queue and the due outbox: they all survive or
all roll back to the instant before the tick, and a test patches each mutation
step in turn to prove it. The scheduler never calls `WorldState.advance_time()`
itself — that would be a clock change with no record in world history, and world
history has to be able to explain why the clock reads what it reads. A test walks
the module's AST to keep it that way.

Due records are durable, not a return value. A one-shot activation is gone from
the queue the moment it fires, so if its `ActivationDue` existed only in the
`TickResult` handed back to the caller, a process that stopped before the
consumer ran would lose that eligibility with nothing left to recover it from.
Every due record is therefore appended, inside the same transaction, to the
session's `ActivationOutbox` (`pns/models/activation_outbox.py`), where it stays
readable until it is explicitly acknowledged. Identity is derived, not random —
`due_id` is `"<activation_id>@<fired_at>"`, so the same record has the same id
after a restore, which is what makes acknowledgement idempotent: the first
`acknowledge()` returns `True`, later ones return `False`, and acknowledging an
id the session never produced raises rather than being silently absorbed.
Acknowledged records are marked, never deleted, so "already handled" stays a fact
that can be checked instead of an absence that has to be assumed.

A clock tick is scoped `public` with no location and no channel, so the exposure
layer decides that nobody perceived it. Characters perceive events, not time.

Recurrence is interval arithmetic from the item's original due time, never from
"now", so a daily 07:00 activation stays at 07:00 instead of drifting later every
time it fires; midnight, month, year and leap-day boundaries are ordinary
`timedelta` arithmetic with no special cases. When one advance steps over several
occurrences they are coalesced into a single due record whose
`missed_occurrences` says how many were passed — skipped occurrences are stated,
never silent. Cancellation is explicit and idempotent: `cancel()` returns `True`
when it removed a pending item and `False` when there was nothing to remove
(never scheduled, already fired, already cancelled), and it never reaches back
into due records or events that already happened.

Scheduler state is **runtime authoritative state**, not configuration. Each
session owns one `PersistentScheduler` over its own `SessionState`; there is no
process-level queue, `ContentRegistry` has no field that holds one and no method
that writes one, and a reload — successful or failed — cannot alter a live queue
or clock.

Serialization has one boundary, not two. `SessionState.to_dict()` carries the
queue and the outbox in a `scheduler` section, and `PersistentScheduler.to_dict()`
returns exactly that section, so a session archive cannot save the world clock
while quietly losing the schedule. `SessionState.from_dict()` rebuilds the whole
authoritative state — world, events, observations, exposure decisions, turns,
histories, queue and outbox — and refuses an archive whose parts come from
different moments: an archive with no `scheduler` section, events or observations
later than the world clock, an activation that is not strictly in the future, a
due record fired after the clock, a scheduler section whose clock disagrees with
the world, a reordered queue, duplicate ids or sequences. A failed restore
installs nothing.

"Persistent" here means exactly that much: the state has one archive shape that
round-trips and is validated on the way back in, and a restored session resumes
with its pending due records intact. Nothing in this phase writes that archive to
disk or reloads it at process start — that lifecycle belongs to the persistent
world stage, and until it exists the durability guarantee stops at the archive
boundary.

The deterministic research round robin is untouched. `SessionRuntime` owns a
scheduler because scheduler state is per-session runtime state, but the turn loop
does not consult it: reproducible research runs depend on the clock standing
still and turn order following the character list, and "the moment arrived, does
this character act?" is an Agency question rather than a scheduling one.

### Agency boundary

`pns/runtime/agency/` answers exactly one question: **given what this character
currently perceives and is eligible to do, do they choose to act, and which
declared action do they propose?** The scheduler decides when the question is
asked, Exposure decides what the character could perceive, and the Router
evaluates behaviour that has already been produced. None of those collapse into
this layer, and none of them collapse into a single model call.

Actions come from a typed catalogue, never from a dictionary.
`pns/models/action.py` defines `ActionId` — `speak.here`, `message.send`,
`presence.join_channel`, `presence.leave_channel`, `movement.move_to` — and one
`ActionDefinition` per member declaring the event type it commits, its
propagation scope, its target kind (`none` / `location` / `channel`), its
preconditions, and the payload keys it accepts. A proposal carrying a key the
definition does not declare is refused outright rather than silently trimmed, and
the event builder filters by the same declaration, so "an arbitrary dictionary
cannot mutate `WorldState`" holds at two independent points. The catalogue stays
small for the same reason `EventType` does: an action counts as implemented only
when the commit boundary knows what the world should look like afterwards.

Preconditions are a closed enum in the model layer and a single evaluator per
member in `pns/runtime/agency/preconditions.py`, mirroring how `ExposureReason`
and the exposure rules are split. Legal options are enumerated by running those
same evaluators over catalogue × candidate targets, so the enumerated set is by
construction exactly the set whose preconditions pass — there is no second,
faster path that could disagree with the first. Ordering is `(action_id,
target_id)` and does not depend on dict iteration.

`AgencyContext` is character-scoped by construction. It is built from a world, a
character id, that character's eligible `ActivationDue`, and **that character's
own observations** — nothing else is passed in, so the builder has no session to
reach through. It therefore cannot read the exposure decision log (a denial
reason is itself information: not knowing something has to include not knowing
that you were denied) and cannot read the omniscient event store. An AST test
keeps `context.py` free of both attribute names, and keeps the exposure log out
of the whole package.

Proposal and commitment are separate calls, not separate comments.
`AgencyEngine.propose()` is pure: it builds the context, asks the policy, checks
legality, and returns a `ProposalPlan`. Nothing is written — the due record stays
pending, and a plan that is never committed leaves no trace. `commit()` is the
transaction. The split is what makes "a proposal is not world truth" a callable
boundary rather than a description, and it is also why every gate is rechecked at
commit time: because a caller may legitimately propose a batch and commit it
item by item, a check performed only during `propose()` can be walked straight
past.

At commit, four things are re-decided against the live world: the session action
ceiling, whether the clock still reads what it read when the plan was made,
whether the proposal's identity has since been claimed, and whether the declared
preconditions still hold. A moved character, a changed channel roster, a sleeping
actor, or an advanced clock all produce `rejected_stale`. The clock rule is
deliberate: an activation asks "at *that* moment, do you act?", and answering it
after time has moved would execute an old decision the character never had a
chance to revisit.

Outcomes are a closed set — `acted`, `abstained`, `rejected_illegal`,
`rejected_stale`, `rejected_budget`, `rejected_policy_error` — and every rejection
has identical consequences for the world: no event, no observation, no partial
state. They are distinguished so that *why* nothing happened is a fact that can
be looked up. "Do nothing" is `abstained`: a valid outcome, not an error and not
a fabricated line. It is recorded, because "evaluated and chose not to act" and
"never evaluated" are different facts for everything downstream.

Policies propose; they never decide. `AgencyPolicy.decide()` receives the
immutable context and returns a `PolicyDecision`, and deterministic
implementations (`AbstainPolicy` — the default, `FirstLegalActionPolicy`,
`ScriptedPolicy`) exist so tests and research runs never depend on a model.
`ModelBackedPolicy` is an adapter over a caller-supplied selector: it translates
the selector's raw shape into proposals and does not judge legality, because
legality is the engine's job and having two places decide what is acceptable —
one of them fed by a model — is how the authority leaks. It is constructed with a
callable and nothing else, so it holds no session, no world, and no ability to
commit. A selector that returns garbage, names an unknown action, or raises
becomes a recorded rejection, never an exception through the transaction.

Budgets are explicit, deterministic, and each one has a branch behind it:
proposals accepted per activation, the size of the legal-action enumeration
handed to a policy (truncation is deterministic and flagged in the context, so
"did not choose" never quietly becomes "was not offered"), how many observations
a policy sees, and how many actions a session may commit in total. That last
count is derived from the audit log rather than kept in a counter, so an archive
round trip cannot hand a restored session a fresh allowance.

Actions that require authored text — `speak.here` and `message.send` — have **no
commit path in this phase**, and that is a boundary rather than a default. A line
becomes world truth through generation → Router evaluation → drift audit →
commit; that chain is not wired into the agency path yet, so committing an
authored line here would put dialogue into world history that no consistency
evaluation ever saw, which is exactly what the commit boundary excludes
elsewhere. There is deliberately no flag that reopens it: a safety boundary a
caller can flip is not a boundary, and "the caller accepts the gap" is not an
authority the caller has. The refusal exists at three points — the policy's
proposal is rejected, a hand-built plan is rejected at commit, and
`agency_event_fields()` (the only function that builds an agency event) refuses
outright, so no code path exists at all. The action schema stays in the catalogue
and stays in the legal enumeration, because the generation layer will wire into
it unchanged and the gate disappears on its own at that point; deterministic
policies simply never select an action that needs a line they would have to
invent.

Scheduler-to-agency handoff happens exactly once. The engine only accepts an
`ActivationDue` that is present in this session's outbox, still matches the
stored record field for field, and has not been acknowledged; the audit record's
identity *is* the `due_id`. Acknowledgement and the audit write happen inside the
same `SessionState.atomic_commit()` as the committed event, so a failure anywhere
leaves the due pending and retryable, and success makes a second evaluation
impossible. Note that `due_id` derives from activation id and firing time and
carries no session number: two identically-stated sessions produce identical due
records, and in that case the engine still consumes the one in its own outbox.

Ownership follows the scheduler. The `AgencyLog` belongs to `SessionState`, the
engine is a service over it, and a session binds exactly one — two engines would
give one eligibility two mutually invisible conclusions, with the first to land
having already acknowledged it. The archive shape is defined once, in
`SessionState.agency_archive()`, and `session.to_dict()["agency"]` is that
section. An archive missing the section is refused rather than restored as "never
evaluated anything".

Restoring validates against the rest of the session, not just against itself. A
record may not be decided after the world clock or before its own activation
fired, must reference a due record this session actually produced and
acknowledged, and must agree with that due record about which character it
concerns. For an `acted` record it is not enough that the referenced event
exists, or even that its id matches the one derived from the proposal: the id is
*derived from* `proposal_id`, so keeping it correct while rewriting the event's
actor, type, scope, landing point, payload or provenance produces an archive
where the audit says one thing, world history says another, and both agree on the
identifier. Restore therefore checks the event's **content**, through
`verify_agency_event()`, which rebuilds the expected fields with
`agency_event_fields()` — the same function that constructed the event in the
first place — and compares them. There is one definition of what an agency event
must look like, so verification cannot drift looser than construction without
construction drifting with it. Actor, event type, scope, landing point, payload,
`correlation_id`, the occurrence time (which must equal the decision time), and
every field of the agency provenance block — `due_id`, `activation_id`,
`proposal_id`, `action_id`, `session_id`, `policy` — are all checked.

Two things genuinely cannot be re-derived from an archive: where a no-target
action's actor was standing, and the commit-time presence roster. Verification
does not replay state effects to recover them — replaying would ask "does the
world still turn out this way?", which is both dangerous and meaningless once the
world has moved on. Instead each is constrained by rule and then taken as given.
`ActionDefinition.participants_from` declares where a roster comes from
(`none` / `channel_members` / `location_occupants`), the builder and the verifier
both read that declaration, an empty roster is checked exactly, and a snapshot
roster is still constrained by the action's own preconditions: an action
requiring "already in the channel" cannot have produced an event whose snapshot
omits its actor, and one requiring "not yet in the channel" cannot have produced
one that includes it. `causation_id` is excluded from the comparison — it links
world history rather than describing the action, and `EventStore` already owns
ordering.

Agency is a **separate runtime path**, not a new step in the research loop.
`pns/runtime/session_runtime.py` does not import it — an AST test enforces that —
and the deterministic round robin, its turn order, and its standing clock are
unchanged. A research session's archive simply carries an empty agency section.
Driving characters autonomously means constructing an `AgencyEngine` explicitly
with a policy and calling `evaluate_pending()`; unlike the scheduler, the engine
is not created for every session, because the state it manages already lives on
`SessionState` and choosing a policy is not a decision the research path should
make on the caller's behalf.

Agency code and schema are **cold update**. `ContentRegistry` has no field
holding a log, policy, or budget and no method that writes one; a reload —
successful or failed — cannot alter a live engine, its log, or the world it
judges against. Editable action *descriptions* could become reloadable content
later; the authoritative catalogue, the preconditions, and the commit rules
cannot.

What this phase deliberately does not contain: no persistent subjective memory or
recall, no long-horizon goal decomposition (the Planner surface here is the
catalogue's declared targets and scopes, nothing more), no relationship or
emotion model, no feed or Sekai Times projection, and no path by which the Router
decides whether a character acts.

---

### Subjective memory boundary

`pns/runtime/memory/` answers two questions and keeps them apart: **what did
this character retain from what it perceived**, and **what comes to mind now**.
Four data products stay separate — world history (what happened), observation
(what this character perceived), memory (what it retained), recall (what surfaces
in this context). Collapsing any pair of them is how characters end up as skins
over one omniscient database.

The only legal input is one of the character's **own observations**. Not the
event store — the perceivable part of an event is already in the observation, and
the rest is precisely what the character cannot know. Not the exposure log — a
denial reason is itself information, and not knowing something has to include not
knowing you were denied. Not another character's observations or memories. The
encoder takes `Observation` objects and refuses any that is not field-for-field
the one this session's `ObservationLog` holds, so a hand-built observation, a
tampered one, or one belonging to another session cannot grow a memory. AST tests
keep `.exposures`, `ExposureLog` and `ExposureDecision` out of the whole package,
and keep `.events` out of the rule, recall and projection layers.

Memory classes are a closed set and each one has behaviour, not just a label.
`MemoryClass` declares, per member, a decay window, whether recall budget may
evict it, and its recall weight:

```text
working      decays after 120 simulated minutes   weight 10
episodic     no decay                             weight 20
semantic     no decay                             weight 25
relational   no decay                             weight 30
identity     no decay, pinned                     weight 50
commitment   no decay, pinned                     weight 60
```

Every member has a real encoding rule behind it, a real prompt tag, and tests
that produce each one from an actual observation. Encoding is a whitelist over
observed event types: dialogue, messages, channel presence and movement encode;
anything unregistered encodes nothing and says so with a reason code. A clock
tick never even reaches this layer — exposure produces no observation for it.

Eligibility, content and salience are all computed from the observation alone,
and all three are declared **in the model layer** next to the record type, not in
the encoder. That placement is the point: restore has to re-decide eligibility,
restore lives in `SessionState`, and models may not import runtime. One
declaration, used by construction and by verification, so verification cannot
drift looser than construction.

Every rule input therefore has to be recoverable from the observation. An earlier
draft let the encoder hold an owner-alias table so that "was this said to me"
could recognise display names; that was removed, because a signal only the
encoding moment knows cannot be recomputed at restore, which silently makes that
class's eligibility *whatever the archive says*. Addressing is currently
recognised by character id in the observed text or by explicit participant
naming; for display names to count, they must first become a verifiable part of
the observation rather than a table in the encoder's hand.

An overheard line leaves only a short-term trace; being addressed, acting
yourself, or uttering a declared commitment marker raises salience past the
episodic threshold and adds the durable classes. The commitment detector is a
**declared marker table**, not semantic understanding: it is deliberately shallow
so that it is deterministic and testable, and the cost — rephrasing evades it,
quoting someone trips it — is written down rather than implied.

Stored text is never a transcript, and that holds for short lines too. A memory
keeps a structural description of what was said (its scale) plus at most a
bounded distinctive fragment: `memory_fragment()` caps the fragment at
`FRAGMENT_CHARS`, *and* at half the original, *and* drops it entirely below
`MIN_FRAGMENT`, so no utterance of any length can be reconstructed from memory.
"Fits under the cap" is not a licence to copy — the exact text lives in world
history for auditing, which is a different data product (§18).

Identity is derived, never random: `owner@event#class`, with the source
observation identified as `observer@event`. Re-encoding the same observation
therefore computes the same id and the store's uniqueness constraint turns a
retry into a recorded `skipped_duplicate` — idempotency by construction rather
than by a flag. World facts get a second idempotency rule keyed by `(owner, fact,
value)`: re-observing that a character is still where you already thought they
were stores nothing; a changed value is a new memory.

Encoding and recall are separate steps, and the split is enforced rather than
described. Stored records are immutable and the store is append-only, so a prompt
asking differently cannot rewrite what was stored. **Decay happens at projection
time**: an expired working trace stays in the store byte-for-byte and simply
stops being returned. Nothing in the recall path writes.

Recall is character-scoped by construction. `recall()` takes a sequence of
memories and a query, and raises if a record belongs to anyone but the querying
owner — the narrowing to `store.for_owner(...)` is one explicit, auditable line
in the service, the same shape as `build_agency_context`. Scoring is integer-only
(class weight, salience, recency band, cue hits capped, counterpart match) and
ordering is a total order — score, then age, then id — so there are no ties and
no dependence on dict iteration or floating point. The budget is explicit: total
items, items per class, and slots reserved for the pinned classes, with a two-pass
selection so a tight budget cannot squeeze out a commitment. Truncation is
flagged, because "did not come to mind" and "never happened" are different facts.

The prompt projection is a whitelist. Memory and event ids, exposure reason
codes, salience and scores, and provenance never appear in it — system process is
not character experience. One observation legitimately produces several typed
memories with different persistence behaviour, but repeating the same sentence
once per class is worthless in a prompt, so the projection collapses identical
bodies into one line and merges their tags. The collapse is presentational; the
store keeps every record.

Memory writes participate in the same atomic boundary as the observations they
come from. `SessionState.atomic_commit()` now covers the memory store as well,
and `MemoryEncoder.commit_and_encode()` puts commit and encoding inside one
transaction: if encoding fails, the event, its observations and its exposure
decisions roll back with it, and a rolled-back memory cannot reach a prompt
because the projection reads the store. A session binds exactly one encoder, for
the same reason it binds one scheduler and one agency engine.

The archive section is versioned. `session.to_dict()["memory"]` carries
`version`, the clock it belongs to, and the store; `MEMORY_ARCHIVE_VERSION` is
currently `2`. Restore refuses an unknown version outright rather than trusting
records that may have been derived under different rules — **the derivation rules
are part of the storage format**, so changing `memory_content()`, the fragment
rule, the eligibility rules or the salience formula requires bumping the version
and writing a migration note here.

- **1 → 2**: fragments replaced verbatim short summaries, and restore began
  re-deciding eligibility and salience. Version 1 existed only on this unmerged
  branch and was never released, so it is refused rather than upgraded in place.
- **Archives predating this phase** have no `memory` section at all. The policy is
  deliberate refusal, not a silent default: restoring them as "remembered nothing"
  makes a session that lost its memory indistinguishable from one that never
  encoded any. The error names the stanza to add — `{"session_id": …, "version":
  2, "clock": …, "store": {"records": []}}` — which is exactly what a session that
  never encoded anything produces, so upgrading is a decision a person makes once
  rather than one the restore path makes on their behalf.

Restore validates against the rest of the session, not just against itself. A
record may not be encoded after the world clock or before its own observation,
must reference an event this session actually committed and an observation this
session actually produced for that owner, and must appear in non-decreasing
encoded order. As with the agency log, matching ids is not enough: ids are
*derived from* the fields, so keeping them correct while rewriting what was
remembered produces an archive where memory says one thing, the observation says
another, and both agree on the identifier. Restore therefore checks, through
`verify_memory_against_observation()`, four things — and leaving out any one of
them admits a forgery that looks legal field by field:

1. identity and timing (whose memory, which event, when it was perceived);
2. **class eligibility**, recomputed with `eligible_classes()`. Content alone is
   not enough: `memory_content()` will happily produce a valid-looking
   `commitment` body for *any* utterance, so relabelling an overheard remark and
   recomputing its id and content yields a never-decaying, budget-proof "promise"
   whose every field checks out. Independent review found exactly that;
3. **content**, rebuilt with `memory_content()` — the same function that
   constructed it;
4. **salience**, recomputed with `derived_salience()`. It is derived, not
   assigned, so forcing it to 100 to dominate recall is caught too. Only the
   `encoder` name in provenance stays unverified, the way `policy` does in the
   agency log; the perception channel it records is checked against the
   observation.

Because eligibility is derived from the observation, the source chain has to be
verified one step further down as well. Rewriting the *observation's* line in an
archive would make a forged class genuinely eligible and leave memory and
observation perfectly consistent with each other, so restore also checks the
fields the rules read — type, actor, text, participant list — against the
committed event they were projected from. What remains outside any in-session
check is an archive whose event history was rewritten wholesale and made
self-consistent throughout; that needs provenance stronger than validation, and
is recorded here rather than implied.

Memory is a **separate runtime path**. `pns/runtime/session_runtime.py` does not
import it — an AST test enforces that — and the deterministic round robin, its
turn order and its standing clock are unchanged; a research session's archive
simply carries an empty memory section. Encoding means constructing a
`MemoryEncoder` explicitly. Memory schema and algorithms are **cold update**:
`ContentRegistry` has no field holding a store, budget or threshold and no
method that writes one, so a reload — successful or failed — cannot alter a live
memory. Editable thresholds or prompt templates could become reloadable content
later; the record schema, the derivation rules and the archive validation cannot,
because P7 replaces configuration snapshots and must never replace live memory
state.

What this phase deliberately does not contain: no vector database or embedding
retrieval (nothing here needs approximate similarity yet, and an opaque index
would end the determinism guarantees), no LLM-driven encoding or recall decisions,
no emotion or relationship simulator (`relational` records who did what to whom,
it does not model how anyone feels), no consolidation or forgetting beyond the
declared decay window, no social feed, media generation or Sekai Times projection,
and no path by which memory mutates events or observations.

---

### Autonomous runtime boundary

`pns/runtime/autonomy/` is the one coordinator that connects P4–P10 into a loop:

```text
scheduled activation
  → character-scoped agency proposal
  → generation, when the action requires authored text
  → Router evaluation and generation audit
  → validated event commit
  → exposure / observation
  → subjective memory encoding
  → a terminal outcome for that activation
```

It **integrates** those authorities rather than replacing them. Scheduler, agency,
event commit, exposure and memory keep their own state, their own validation and
their own transaction boundaries; the coordinator owns none of them. It cannot
even store an activation — that lives in `SessionState`. A session binds exactly
one coordinator, for the same reason it binds one scheduler, one engine and one
encoder: two of them would draw from the same outbox, run generation twice and
commit into the same history, and "how was this activation handled" would have
two self-described authoritative answers.

**Authored text now has exactly one path, and it is a credential, not a switch.**
Through P10 there was a structural gap: `_require_committable()` refused every
action needing authored text, because the generation → Router → audit → commit
chain did not exist, so no code path could write an unjudged line into world
history. P11 builds that chain, so the gap becomes a gate — but not a boolean
one, because a boolean is something a caller can flip, which makes it a
suggestion rather than a boundary. What the gate demands is a typed
`GenerationAudit` that

1. **binds** to the proposal — same proposal id, same character, and the
   **whole payload** byte for byte; and
2. is **accepted** — score below threshold and not flagged for human review.

Binding covers the entire payload, not just the line. Adversarial review found
the narrower version: with only `text` bound, an audited line could keep its text
and have `char_name` swapped, and since observers render `char_name` as the
speaker, every observation of that event named the wrong character. The line was
judged; the identity attached to it was not. Binding the payload closes that and
covers any authored key added later.

The verdict is **derived, never assigned**: `is_ooc` is recomputed from score and
threshold, and the judge's own `is_ooc` has no storage location at all, so a
judge claiming a 9-point line is fine changes nothing. The scale is capped at 10
and so is the threshold, so no threshold setting accepts a 10. A line the judge
itself flags for human review is not accepted either: the research path has a
person watching the screen and records OOC turns with a correction, and the
autonomous path does not, so an uncertainty there cannot be silently promoted to
certainty. Audit failure is never audit success — a judge that raises, or returns
something that is not a credential, produces a recorded failure, never a pass.

Three gates enforce this, and only the innermost one is structural:
`AgencyEngine.propose()` deliberately does *not* refuse authored proposals (the
judge has to see the line, so the line must be proposed first); `commit()`
refuses any authored proposal whose credential is missing, unbound, unaccepted or
from another clock; and `agency_event_fields()` — the single function that turns
a proposal into an event — refuses again. So `AgencyEngine.evaluate()`, which is
propose-plus-commit with no audit step in between, still reaches exactly the P9
conclusion for dialogue: `rejected_illegal / authored_text_not_committable`. The
direct agency path did not gain a way to speak.

The credential is persisted twice on purpose: in the event's `provenance` and in
the agency record's `detail`. Restore rebuilds the provenance from the record's
copy through the same construction code, so softening the score on either side,
or deleting it from either side, produces a mismatch. What no in-session check
can catch is a caller who fabricates an accepted credential in the first place,
or an archive rewritten consistently on both sides — the Router's judgement is
not re-derivable offline. That is the same class of residual as the unverified
`policy` string in the agency log and the `encoder` name in memory provenance,
and it is recorded here rather than implied.

**Model input is character-scoped, model output is untrusted.**
`GenerationContext` is built by transcription only, from an already-narrowed
`AgencyContext` plus that character's own recall; it takes no `SessionState`, so
there is no path to another character's memory, to the exposure log, or to
omniscient event payloads. The due activation reaches it as `ActivationCue` —
kind, when, and an explicitly declared `cue` string — never as the `ActivationDue`
record. Independent review found the earlier version handing over the whole
record, which put arbitrary scheduler `payload` into model input; sanitising
`to_dict()` would not have fixed it, because the generator still held the object
and could read `context.activation.payload`. A character does not know it has a
schedule row, so due ids, queue sequence numbers, missed-occurrence counts and
next-due times are gone as well, and the payload whitelist is one key wide:
content authors put character-visible text under `cue`, and a malformed or
over-long cue fails loudly instead of being truncated. AST tests keep `.events`, `.exposures`, `.memories`,
`.agency` and the name `SessionState` out of the module. Its `to_dict()` is a
whitelist, not `Observation.to_dict()`: review found the raw projection carried
the exposure reason code. That code is never a denial — a denial produces no
observation — but it is still exposure bookkeeping rather than character
experience (§15), the same reason the memory prompt projection drops provenance.
Going the other way, output is one line and nothing else: a plain string or
`{"text": ...}`, with any other key refused rather than dropped, capped in length,
and never carrying `character_id` or `char_name` — a model that names itself is a
model choosing who it is playing, and the display name comes from configuration.
The parsed line becomes an `ActionProposal`, which the engine then revalidates for
identity, action, target, preconditions and budget like any other.

**One activation is one transaction.** Generation and judging are pure and happen
*outside* any transaction, so slow calls never hold one open. Everything
authoritative — event, exposure decisions, observations, agency record,
acknowledgement, and memory encoding — goes inside a single
`SessionState.atomic_commit()`. Fault injection at the event append, the agency
append, exposure, memory and acknowledgement each leaves the world byte-identical
to before, with the activation still pending. Retrying then commits exactly once:
proposal ids are derived from the due id, event ids from proposal ids, and the
handoff is single-use, so duplication is refused by construction rather than by a
guard.

**Every due activation gets an answer.** The outcome codes are `acted`,
`abstained`, `rejected`, `failed_retryable`, `failed_terminal` and `stopped`. The
first four of those are terminal; the durable form of a terminal outcome is not
the result object but the session itself — an agency record exists for that due
and the outbox has acknowledged it. `failed_retryable` and `stopped` are exactly
the absence of both, so in an archive they appear as "still pending" and are
picked up again, never silently lost. `stopped` is a sixth code rather than being
folded into `failed_retryable` because a deliberate shutdown is not a failure, and
merging them would make the status panel lie. The retry budget is explicit and
finite; when it runs out the coordinator writes a terminal-failure record and
acknowledges, because "did not succeed" is an acceptable ending and "nobody knows"
is not. If even that recording fails, the result says `stuck` and reports the due
as still pending rather than dropping it. Retry counts live in the process, not
the archive — cross-restart persistence is P12 — and losing them restarts the
budget, not the commit, which is fenced separately.

**The lifecycle has one linearization boundary.** `start()`, `stop()` and "may
this write proceed" all take the same lock, and each does its check *and* its flip
under a single hold. Checking outside the lock is not enough, and review found
both halves of that: a `stop()` landing between `start()`'s check and its flip
yields a runtime that is running and stopped at once, and two concurrent
`start()` calls both succeed. Re-checking a running flag after each slow call has
the same shape of hole on the commit path — between that check and entering the
transaction a stop can land and an already-judged proposal still commits. So
admission is a locked decision too. The checks after generation and judging
remain, but only as an optimisation that skips a doomed round trip; the
authoritative refusal is the locked one.

Slow calls hold nothing. Generation and judging run outside the lock, so a hung
model call cannot block a shutdown — a deterministic test parks a worker inside
the generator and asserts `stop()` still returns promptly.

**`stop()` has two returns, and the difference is the whole contract.** An
*effective* stop reports `running: False`, and the guarantee is exact: after it
returns, no further commit can land — not the event, not the agency record, not
the acknowledgement. It gets that by waiting on the lock until any transaction in
flight has completely finished, because all three of those writes live inside it.
Every call from outside a transaction is this kind.

A *deferred* stop reports `running: True` with `stop_requested: True` and
`stopping: True`. It happens only when the caller is already inside the
transaction, where waiting would mean waiting on itself. Review found the earlier
code claiming an immediate stop there: reentrancy let `stop()` return at once
while the enclosing commit went on to write the agency record and the
acknowledgement — a stop that returned, followed by a commit. Deferral is the
honest answer. The request is recorded, the transaction runs to completion
(tearing it open would leave half an event, which is worse than stopping one
activation late), and the stop takes effect the instant that transaction ends —
including when it ends by rolling back, because it was still requested. Only then
does `running` go false, and only then are new activations refused.

So the invariant holds in one sentence either way: **`running` is false only at a
point after which nothing more can commit.**

`status()` is read under the same lock and is therefore a consistent snapshot —
it never shows `stopping` disagreeing with `stop_requested and running`, or a
runtime that is running without having started. The bare `running` and
`stop_reason` properties deliberately do not take the lock: they are instantaneous
reads, and locking them would make "is a commit in flight?" unanswerable, since
asking would block behind that very commit.

The same lock also registers in-flight activations, so one due cannot be
processed twice concurrently — that would not double-commit (handoff is
single-use and ids are derived) but it would run generation and judging twice and
hand the loser a confusing handoff error. Naming a due explicitly through
`process_due()` is refused loudly; the batch driver `process_pending()` skips it
instead, because there the due is not lost, someone else is holding it. A clock
tick is authoritative too and takes the same lock; a stop landing between the tick
and the processing leaves every due sitting in the outbox, advanced clock and all.

A runtime that was stopped can never be started, including one stopped before it
ever ran — the test is whether a stop was ever requested, not whether it is
currently running, because the weaker test yields a runtime that is simultaneously
running and stopped.

**The research path is untouched.** `session_runtime.py` does not import this
package (an AST test enforces it), and the stronger statement also holds: a
coordinator attached to a live research session's `SessionState` changes not one
WebSocket message, turn, history entry or clock value of that session's
deterministic round robin, because the round robin never advances the clock and
therefore nothing ever falls due. Importing the package initializes no reload
boundary, holds no module-level live state, and pulls in no HTTP or model SDK:
generators and judges are injected, so the complete loop runs offline and
deterministically, and the determinism claim covers the real adapters because they
travel the same parsing and validation channel as the scripted ones.

Orchestration is **cold update**: `ContentRegistry` has no field that reaches a
coordinator, a scheduler, an engine or an encoder, so a reload — successful or
failed — cannot alter a world already running.

What this phase deliberately does not contain: no WordPress or Sekai Times
publishing, no Dashboard redesign (the service surface here is start / stop /
status / simulated clock / positions / recent outcomes and events, and nothing
more), no concrete 25ji content or schedules, no cross-restart persistence of
in-flight activations, no relationship or emotion simulation, and no path by
which the coordinator writes state that its constituent services do not already
own.

---

### Persistent world lifecycle boundary

`pns/runtime/persistence/` gives one autonomous world a complete process
lifecycle:

```text
create or restore
  → acquire exclusive ownership
  → bind runtime services from caller-supplied cold adapters
  → run / checkpoint at safe boundaries
  → stop and let in-flight work settle
  → write one complete atomic archive
  → release ownership
  → restore the same authoritative world after restart
```

What it persists is the authoritative `SessionState`: world state, event history,
observations, exposure decisions, the activation queue and outbox, the agency
audit log and subjective memory. What it never persists is anything alive —
service instances, model clients, API keys, locks, callables. That is enforced
structurally, not by convention: capture walks the payload and refuses any value
that is not plain JSON data, because `metadata` is a free-form dict and anyone
can drop a client into it.

**The durability contract is small enough to state exactly.** A save writes a
temporary file in the destination directory, flushes it, `fsync`s it, atomically
`os.replace`s the target and then syncs the directory. At every instant the disk
holds either the previous complete archive or the new complete archive — a
half-written file is always still a temporary file, and a temporary file is never
named `world.json`. A failed save leaves the previous archive untouched and
reports whether temporary material remains that a human has to clear. Crash
recovery reads the last successfully replaced archive and ignores — but reports —
incomplete temporaries.

**Visible and durable are two different claims, so they get two different
answers.** `os.replace` makes the new archive visible; the directory `fsync`
makes that rename survive a power loss. When the directory sync genuinely fails
— `EIO`, `ENOSPC` — the save does not return success: it raises
`ArchiveNotDurable`, which says exactly what happened, namely that this revision
*is* on disk and readable but may not come back after a power loss. That is a
different shape from every other storage failure, so the lifecycle books it
differently: the revision advances, because the disk really does hold that
revision and reusing the number would let two different contents share it, while
`durable: false` and the error stay in the status and the close refuses to call
itself clean. That evidence belongs to the live filesystem operation, not to the
archive payload: after a later restore, `durable` and `directory_synced` are
`null` (unknown) until this process completes another checkpoint. Merely being
able to read an archive must not silently upgrade a previously unproven save to
durable. A platform or filesystem that simply has no directory `fsync`
(Windows cannot even open a directory handle; some filesystems answer `EINVAL`)
is the other case entirely — nothing failed, the capability is absent — so the
save succeeds and says `directory_sync_supported: false`. The errno list that
separates the two is a whitelist: an unrecognized errno counts as a real failure,
because on durability the safe direction to guess wrong is toward reporting a
problem. Permission errors such as `EACCES` and `EPERM` are real failures, not
evidence that the platform lacks the capability.

**The recovery boundary is the last successful checkpoint. Nothing more.** There
is no WAL, no event-sourced replay and no zero-loss crash guarantee, and the
implementation does not pretend otherwise. Work committed after the last
checkpoint is lost on a crash. What survives that loss is *correctness*, not
*work*: an activation that fell due before the checkpoint but was never
acknowledged is simply still pending when the world comes back, so it is
processed again — and processed **once**, because the outbox handoff is one-shot.
Re-running is not double-committing.

**Ownership is two gates, and it needs both.** In-process, a registry keyed by the
resolved lock path refuses to open the same world twice — keyed by path rather
than by a bare `world_id`, so two stores pointing at the same archive root, or a
root reached through a symlink, are still the same world. Across processes, an
exclusive `fcntl.flock` decides, and the decision is the kernel's: the lock dies
with the process holding it. That is why stale-owner recovery needs no pid
heuristics and can never steal from a live owner. A pid comparison would be wrong
in both directions — a reused pid reads as "alive" and locks the world out
forever, and a process exiting mid-check reads as "dead" and produces two owners.
A crashed owner leaves its record behind, so the next owner knows it took over a
crashed world and reports `recovered_from`; a clean release rewrites that record,
so there is nothing to report.

**Checkpoints observe one coherent state, and the exclusion is real.** A
checkpoint takes two locks in one fixed order: the coordinator's gate — the same
one `start`, `stop` and commit admission share — and then the session's own
exclusive boundary, which is the very lock `SessionState.atomic_commit()` holds
for the whole of a transaction. Both are necessary. The gate only covers commits
the coordinator starts, while the scheduler's time advance and the event commit
layer open `atomic_commit()` directly; snapshotting inside such a transaction
produced an archive in which the clock had advanced, a one-shot activation had
been taken off the queue, and its due record had not yet reached the outbox —
that activation was gone for good, and the archive passed every validation.

Asking *whether* a transaction is open is not enough, which is the second thing
review established. A question is answered at an instant; between the answer and
the first byte of `to_dict()` a time advance can start and run alongside the
snapshot, and the archive is torn exactly as before — only now in the other
direction, with the snapshot first. So the two operations share one lock and
serialize: a checkpoint waits for a running transaction to finish rather than
refusing, and a transaction cannot start while a snapshot is in flight. The lock
is taken before `atomic_commit()` builds its rollback snapshots, not after, so
there is no window in which a commit is already underway and the state still
looks idle. Nesting still works (a commit inside a commit) and a snapshot from
inside one's own transaction is refused rather than admitted by re-entrancy.

The order — gate, then session boundary — is the one global rule, and it is
enforced by a bounded wait rather than by hope: a path that violates it would
otherwise hang forever, so the snapshot gives the boundary a deadline, and on
expiry it fails loudly, releases the gate and lets the system unwind. The
snapshot itself is taken inside the boundary; serialization and the write happen
outside it, so one `fsync` never blocks a shutdown.

**Shutdown order is fixed**: stop admission, wait for the running transaction to
settle, checkpoint the final state, mark the handle closed, release ownership. A
failed final checkpoint does not claim a clean close and does not release
ownership — releasing would announce that what is on disk is the latest state.
Abandoning such a world is possible but explicit (`close(force=True)`), and the
status it returns says `clean: false` and names the revision that is actually
recoverable.

Writes also verify ownership first. A lock lives on an inode, so deleting the
lock file out from under a live owner lets the next process acquire the world
while the first still believes it is the only writer — confirmed by attack, two
processes writing the same archive. The check cannot prevent that deletion, but it
turns "two writers silently overwriting each other" into "the second write fails
loudly".

Paths are confined to one configured archive root by two independent checks:
`world_id` must be lowercase ASCII with no separators, no traversal and no
leading or trailing punctuation (uppercase and non-NFC names are refused rather
than normalized, because case-insensitive and Unicode-normalizing filesystems
would fold two different ids into one directory and quietly break the one-owner
rule), and the resolved directory must still sit directly under the resolved
root, which is what catches a symlinked world directory.

The service surface is deliberately minimal and Python-level: list, create,
restore, checkpoint, close, status — including ownership, revision, last
successful save, dirty state, residue and recovery error. No HTTP routes and no
UI: those belong to `WEB-1`, and this phase exists to settle how a world lives,
saves and is owned before deciding what it looks like.

**The research path is untouched.** `/ws/run` acquires no world lock, writes
nothing under the archive root, and nothing in `pns/` imports this package (an AST
test enforces both). Persistence is opt-in by explicit call. Importing the package
performs no I/O, creates no directories, takes no locks and initializes no reload
boundary.

What this phase deliberately does not contain: no database, cloud storage or
multi-host failover, no WAL or replay, no ST-1 publishing, no WEB-1 dashboard, no
concrete 25ji content, and no background checkpoint writer — automatic
checkpoints, when enabled, are synchronous, coalesced and taken only at completed
authoritative boundaries.

### Agent activity boundary

`WorldState.character_activities` is the authoritative answer to “what is this
character doing now?”. Each entry is a typed `CharacterActivity(kind, since)`;
absence means `unspecified`, not “infer the most plausible occupation from the
character pack”. `since` is simulated time and is covered by transaction rollback,
archive serialization and restore validation.

Activity is intentionally a closed enum. It enters character and Router prompts,
so accepting authored free text here would create a prompt-injection path as well
as an unverifiable state vocabulary. A change is represented by
`character.activity_changed`, applied by the normal event commit boundary, and
exposed privately to the actor. The operator HTTP endpoint constructs this typed
event, submits it through the coordinator lifecycle gate and checkpoints it; an
identical retry creates no duplicate event and can finish a checkpoint that failed
after the first commit.

The Nightcord compatibility fixture initializes `online_chatting` because channel
membership is explicit. It does not initialize drawing, composing or video work
from character occupations. Generation and Router receive only the acting
character's current activity. Goals, emotions, schedules and autonomous activity
selection remain separate later product boards rather than fields hidden inside
the activity string.

### Authored daily rhythm

A character pack may declare `daily_rhythm`: an ordered set of segments, each one
a minute-of-day start, one closed-enum activity and an optional known
`location_id`. A segment runs until the next one starts and the last wraps past
midnight, so “which segment is it now” always has exactly one answer. The table is
parsed and validated while the `ContentRegistry` snapshot is built — an unknown
activity, an unknown location, a duplicate minute or any extra (free-text) field
rejects the whole build rather than surfacing at 3am as a failed commit. Segments
carry no prose, and `unspecified` is not a legal segment: declaring “no fact” is
the same as not authoring that segment.

The rhythm proposes; it never writes. `RhythmDirector.plan()` is a pure function
of the authoritative `WorldState`, and the coordinator commits its output through
the existing `character.location_changed` / `character.activity_changed` events in
one atomic transaction inside the lifecycle gate, immediately after a scheduler
tick and before that tick's activations are processed — so a character generating
a line in the same tick already sees the new segment's activity. There is no new
`ActivationKind`, no queue entry, no timer and no new persisted field; a world
whose clock is not advancing has no rhythm transitions.

Whether the rhythm may speak for a character is re-derived from durable state
alone, and the state it reads is the world history: it speaks only while no
`character.activity_changed` or `character.location_changed` event for that
character has been committed at or after the current segment's start. The activity
record's `since` is deliberately *not* the test — it is only a proxy for the last
activity event, and a segment change that moves a character without changing what
they are doing commits no activity event at all, so `since` would stay behind in
the previous segment and the rhythm would keep overriding that character's own
movement for the rest of the day. Location changes are events too, so the history
has no such blind spot.

Three consequences follow. A failed or interrupted transition is retried by the
next tick, because nothing about “already applied” lives in memory or in the
archive. Any decision made inside a segment — an operator activity change, an
agent's own `movement.move_to`, or the rhythm's own transition — leaves an event
inside that segment and therefore closes the gate until the next segment begins;
the rhythm is a default day, not a cage. And a tick that jumps over whole segments
applies only the segment that is current now; skipped segments did not happen and
are not replayed.

Rhythms are bound like every other cold adapter: a world uses the snapshot it was
opened with, so reloading content does not rewrite a world that is already open.
Boundary transitions are deliberately instantaneous and are not `movement.move_to`
actions, so travel time is abstracted into the boundary minute; the cycle is one
24-hour day with no weekday variation. The legacy scene fixture still provides the
initial world state, and the authored 25ji rhythms agree with the Nightcord
fixture at its own hour rather than overwriting it.

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
commit: apply state effect → append event → evaluate exposure
      │
      ▼
observations for eligible characters only
      │
      ▼
project each character's history from its own observations
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

Implemented as a foundation: `pns/runtime/scheduler.py` decides when simulated
time advances and which scheduled activations become due, and emits typed
due records for a later stage to act on. See "Persistent scheduler boundary" in
§3 for the guarantees it makes.

It currently considers:

- simulated time
- a per-session queue of scheduled activations, with recurrence
- a durable outbox of due activations awaiting acknowledgement

It may later also consider:

- character schedule as authored content
- queued events
- location
- availability
- exposure
- pending commitments
- attention load
- autonomous activity
- safety / compute budgets

The persistent scheduler is an evolution of runtime scheduling.

It is not a reason to remove or duplicate the existing research scheduler, and
the round robin still runs unchanged next to it.

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
7. Draw the configuration reload boundary
        ↓
8. Introduce persistent scheduling
        ↓
9. Introduce Agency / Planner
        ↓
10. Introduce subjective persistent Memory
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
