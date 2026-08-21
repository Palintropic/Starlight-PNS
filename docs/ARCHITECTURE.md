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
│   └── DriftScore
│
├── pns/runtime/
│   ├── SessionRuntime      (session orchestration)
│   ├── event_commit        (the commit boundary)
│   ├── exposure/           (eligibility + observation projection)
│   ├── content_registry    (the single configuration build entry point)
│   ├── reload              (the configuration reload boundary)
│   ├── scheduler           (simulated time + due activations)
│   └── agency/             (declared actions: propose, validate, commit)
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

One budget is a safety gate rather than a limit. `allow_authored_text` defaults
to **off**, which closes `speak.here` and `message.send`. A line becomes world
truth through generation → Router evaluation → drift audit → commit, and that
chain is not wired into the agency path in this phase; committing an authored
line here would put dialogue into world history that no consistency evaluation
ever saw, which is exactly what the commit boundary excludes elsewhere. Opening
the gate is possible and explicit, and it means the caller accepts that gap.

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
section. Restoring validates against the rest of the session, not just against
itself: a record may not be decided after the world clock or before its own
activation fired, must reference a due record this session actually produced and
acknowledged, and an `acted` record must point at an event that exists in world
history. An archive missing the section is refused rather than restored as "never
evaluated anything".

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
