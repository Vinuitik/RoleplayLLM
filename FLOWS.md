# FLOWS — how the engine actually works

Written as a handoff document. If you are a new session picking this up, read
this file first; it explains not just what the code does but *why each decision
was made*, because most of them look arbitrary until you know the failure they
prevent.

---

## The one idea

**Secrets are kept by ABSENCE, not by instruction.**

No prompt anywhere says "do not reveal the poison plot." Instead, the model that
voices a character physically never receives the fact. A prompt-enforced secret
fails the first time the player asks an unexpected question. An absent one cannot
fail, because there is nothing to fail with.

Everything below is machinery in service of that idea, plus its two corollaries:

- **Corollary 1 — truth is also a secret.** Even the narrator never learns
  whether a belief is true. A narrator that knew would hedge ("you *think* he
  died of old age"), and that hedge tells the player there is something to doubt
  while disclosing no hidden fact at all. Leak by tone.
- **Corollary 2 — outputs need filtering too.** Projection protects what goes
  *into* a model. `visible_events` protects what comes *out of* the engine. Skip
  the second and the narrator faithfully reports a poisoning three rooms away.

---

## Services

```
  browser ──► nginx (frontend:8081) ──┬── / ........ React SPA + PWA
                                      └── /api/ .... proxy ──► engine:8090
                                                                  │
                                                                  ▼
                                              host-wrapper (HOST :5501, NOT docker)
                                                                  │
                              ┌───────────────┬────────────┬──────┴────┬─────────┐
                            groq          github      mistral      gemini    claude-cli
                                                                              (+ ollama)
```

| Service        | Where            | Why there |
|----------------|------------------|-----------|
| `frontend`     | docker :8091     | nginx serves the SPA *and* proxies `/api`, so the browser sees one origin. That is why there is **no CORS config anywhere** in this project. |
| `engine`       | docker :8090     | All game logic. Loopback-published only; the browser reaches it through nginx. |
| `host-wrapper` | **host** :5501   | Shells out to the `claude` CLI so Claude calls bill the **subscription**, not metered tokens. A container has neither the CLI nor its logged-in credentials. |
| `cloudflared`  | docker, profile  | `--profile tunnel` → free `*.trycloudflare.com` URL. Needed for PWA install (service workers require TLS off-localhost). |

**The wrapper is not in compose on purpose.** If you "fix" that by containerising
it, you silently move all Claude usage onto metered API tokens.

---

## A turn, end to end

`turn.py :: play_turn()`. **The order is the design, not an implementation
detail.**

```
1. REFEREE      llm.get_referee(projected_player_view, action_text)
                → difficulty, opposing stat, archetype, hours_elapsed
                Runs FIRST because it decides how long the turn takes.

2. TICK         plots.tick(world, rng, hours)
                clock → modifiers → meters → plots → expiry
                The world moves whether or not the player acted.

3. PLAYER       resolution.resolve(stat, difficulty, rng)
                Dice decide. The referee only set the problem.
                A `discovery`-tagged action also searches for evidence here.

4. ONSTAGE      scene.run_scene(mode=ONSTAGE) for the player's room
                A GROUP RELAY, not a queue of monologues. Runs in passes;
                within a pass each person speaks having heard everyone
                before them. THE ONLY EXPENSIVE STEP:
                `people_present × passes` cheap calls.

5. OFFSTAGE     scene.run_offstage() for rooms with pressure
                ZERO TOKENS. Disclosure, propagation and belief revision
                are all pure engine code, so a conversation the player
                cannot perceive needs no model at all. Produces a
                SceneRecord the player may later discover.

6. FILTER       turn.visible_events(world, events, player_id)
                ◄── THE STEP PEOPLE LEAVE OUT. Without it everything above
                    leaks through the narrator.

7. NARRATE      llm.get_narration(projected_view, perceived_events)
                Last, and fed only already-decided, already-filtered facts.
                A mouth, not a brain.
```

**There is no orchestration framework.** No LangGraph, no agent runtime. The
sequence above *is* the graph, written out as Python. Each head is a function
taking a `ProjectedWorld` and returning a validated Pydantic model, and the only
shared state is `WorldState` — which no model ever sees.

That is deliberate. A graph framework's main affordance is passing context along
edges, and this design's core invariant is that the heads must **never** share
context: every boundary is a `project()` call that *deletes* information. A tool
that makes state-passing easy would make the one forbidden thing easy to do by
accident.

---

## Module map

| File | Owns | Touches an LLM? |
|---|---|---|
| `models.py` | Fact / Belief / Character / Plot / Clock / Meter / Modifier / WorldState | no |
| `formula.py` | Safe AST evaluator for LLM-authored math | no |
| `numerics.py` | Meters, modifiers, elapsed-time advance | no |
| `projection.py` | `project()` — the wall. `perceives()` | no |
| `resolution.py` | Dice, four outcome degrees, difficulty ladder | no |
| `plots.py` | `tick()`, plot advance, exposure, `propagate()` | no |
| `gametime.py` | Phase of day, `coerce_hours()` | no |
| `disclosure.py` | Whether a character actually says what they meant to | no |
| `revision.py` | Contradiction, belief contests, misinformation | no |
| `evidence.py` | Physical traces; the only route to certainty | no |
| `actions.py` | The action vocabulary, loaded from data | no |
| `combat.py` | Engagements (Lanchester linear law) | no |
| `scene.py` | The group relay; onstage vs offstage | via `llm.py` |
| `telemetry.py` | One row per model call; violation counting | no |
| `worldgen.py` | Session Zero — builds a world before play | via `llm.py` |
| `llm.py` | Wrapper client + every prompt + reply validation | **yes — the only one** |
| `turn.py` | The loop, NPC turns, `visible_events()` | via `llm.py` |
| `store.py` | SQLite snapshots, rewind, truth report | no |
| `main.py` | FastAPI surface | no |

Seven of ten modules are pure and deterministic. That is deliberate: it is what
makes the game testable and the outcomes real.

---

## Data model — why it looks like this

### Facts and Beliefs are separate, joined by a table

```python
Fact   { id, content, is_true, tags }
Belief { char_id, fact_id, stance, confidence, source_char_id, turn_acquired }
```

- **Not `known_by[]` on the fact.** The edge carries data. `source_char_id` is
  what makes "Orys believes X *because Varys told him*" representable — and
  therefore makes a discovered lie unwindable by invalidating one edge.
- **`is_true` is ENGINE-ONLY.** It is never copied into a projection, never
  prompted, never narrated. Its only legitimate consumers are the engine (an
  action premised on a false belief should fail) and `store.truth_report()`
  after the game.
- **`content` is phrased neutrally** — "Aerion died of poison", never "X thinks
  Aerion died of poison" — because the same fact is shared by everyone who holds
  it, each at their own confidence.

### Mutable state is NOT a fact

`alive`, `location`, and meters are fields that change. A fact is a discoverable
proposition with a truth value. Conflating them means writing fact-invalidation
logic forever.

---

## Projection — the wall

`project(world, char_id) -> ProjectedWorld`

Strips everything the character does not hold a belief about. Three specific
leaks it closes:

1. **Truth.** `is_true` is never read. See Corollary 1.
2. **Identifiers.** Fact ids are hand-authored and therefore descriptive
   (`f_king_poisoned` announces the secret in the id even when content was
   filtered). `ProjectedBelief.fact_id` is `Field(exclude=True)` — present on the
   object for the engine, absent from every serialization.
3. **`hides` is not trusted.** A character can only conceal a fact they actually
   hold a belief about. *The property test caught this one live* — Ollivar's
   `hides` listed a fact he had no belief in, and its content leaked straight
   into his own prompt.

Always serialize prompts through `view.prompt_payload()`, never by reaching into
fields, or the `exclude=True` protection is bypassed.

---

## The belief-index protocol

**How an NPC is prevented from inventing or leaking a fact when it speaks.**

The prompt hands the speaker a numbered list of *their own* beliefs:

```
  [0] The king has been bedridden for eleven days. — you are certain of this
  [1] The treasury is short. — you only suspect this (confidence 0.6), heard from Stagg
```

The reply must be an **index**, not content:

```json
{ "action": "speak", "target": "Byren Stagg", "reveals_belief": 1, "truthful": false }
```

Saying something you don't know becomes *inexpressible* rather than merely
forbidden. An out-of-range index is dropped (`turn.py::_speak`). Note there is no
truth marker in the menu — the speaker doesn't have one either.

---

## Numeric meters

The division of labour:

> the LLM emits a formula or modifier **once, as data**;
> the engine applies it **every tick**, deterministically.

Ask a model to compound a value over twelve turns and it drifts. Ask for
`"gold * 0.02 - upkeep"` and the engine runs it exactly, forever.

- **Advance is by ELAPSED HOURS**, not turn count. `last_advanced_at_hour` per
  meter makes it idempotent — replay and rewind can never double-count.
- **Rates are computed from a snapshot of pre-step values**, then applied
  together. Without simultaneous update, a formula referencing another meter
  would depend on dict iteration order and the same save would evolve
  differently after a reload.
- **`RATE_PCT` modifiers stack multiplicatively** so two −50% debuffs quarter a
  rate rather than zeroing it (and three don't reverse its sign).
- **A bad formula freezes one meter and logs it** — it never halts the turn.

### `formula.py` — never use `eval()` here

LLM-authored formulas are untrusted input executing on the host. The evaluator
walks an AST **whitelist**: numbers, bound names, arithmetic, comparisons,
ternary, and a short list of pure functions. Attribute access, subscripts,
lambdas, comprehensions and unlisted calls all raise `FormulaError`. Unknown node
types are refused by default. Exponents are capped (`2**10**9` would hang).

---

## Time

LLMs forget to advance time. Two mechanisms, neither relying on memory:

1. **`hours_elapsed` has an engine-side default.** `gametime.coerce_hours()`
   clamps to `[0.05, 72]` and substitutes the archetype default when the value is
   missing, non-numeric, or absurd. **Returning 0 cannot freeze the clock** — it
   is raised to the floor. Forgetting is not a state the schema permits.
2. **Phase of day is computed and handed over.** Every projection carries
   `phase` ("night") and `time_of_day` ("day 2, evening (19:30)"). No model ever
   infers whether it is night from a raw hour.

---

## Determinism and rewind

- `resolution.make_rng(seed)` returns a **private** `random.Random`. Never
  `random.seed()` — that mutates global state and makes concurrent games
  interfere.
- `turn._rng_for(world, seed)` derives the generator from **(seed, turn)**. This
  is what makes rewind honest: replaying turn 7 from a snapshot reproduces turn
  7's rolls exactly.
- `store.py` saves the **full world** every turn, so rewind is a `SELECT`, not a
  replay. It doubles as the anti-cheat inspector: diff turn N against N+1.
- LLM replies are *not* deterministic. Rewind restores state, not prose.

---

## Provider routing

`host-wrapper/llm_router.py`, inherited from ObsidianOptimizer.

- **Two chains.** `LLM_TEXT_PRIORITY` (NPC intentions — cheap, schema-constrained,
  quality barely shows) puts free tiers first. `LLM_NARRATOR_PRIORITY` is
  **inverted** — narration is the one call the player reads, once per turn, so it
  gets the good models first.
- Per-provider rate spacing, 429 benching with escalating cooldown honouring
  `Retry-After`, priority queue so important calls win a freed provider.
- `ollama` is last on both chains: reached only when every hosted provider is
  benched or unreachable — exactly the offline case.
- `complete_json` is paranoid by design: `response_format` is only a *nudge*,
  `_parse_json_object` digs the object out of fences/prose, `required_keys` is
  checked, and failures retry with a repair instruction that re-enters the router
  (so a provider that keeps babbling gets skipped).

### Claude CLI resolution

`claude` is installed natively at `~/.local/bin/claude.exe` (v2.1.220), auth is
**OAuth / subscription type `pro`**, and `ANTHROPIC_API_KEY` is deliberately
blank — so Claude usage draws on the subscription and can never silently fall
onto metered tokens.

`_find_claude()` does **not** trust PATH alone. The native Windows installer
drops the binary in `~/.local/bin` and tells you to restart your terminal, so any
already-running process (like a long-lived wrapper) never sees it — and the
failure is silent, quietly shifting spend to other providers. Resolution order:

1. `CLAUDE_BIN` env override
2. `shutil.which("claude")`
3. known install locations (`~/.local/bin`, npm shim, `/usr/local/bin`)

Verified routing after a full turn: `claude-cli ok=2` (narration) and
`github`/`groq` `ok=3` (NPC intentions) — the intended split.

---

## Tests — 84, all deterministic, no LLM

`services/engine/tests/`

The important ones are **properties, not spot checks**, because the leak you
think to check for is never the one that gets you:

- `test_projection_contains_exactly_the_beliefs_held` — for every character and
  every fact, appears in projection **iff** a belief exists. Both directions:
  forward catches leaks, backward catches erasure.
- `test_is_true_never_appears_in_any_projection`
- `test_a_false_belief_is_indistinguishable_from_a_true_one`
- `test_fact_ids_never_reach_a_prompt`
- `test_narrator_never_receives_events_from_another_room`
- `test_npc_cannot_speak_a_fact_it_does_not_hold`
- `test_zero_hours_is_raised_to_the_floor`
- `test_formula_refuses_code_execution` (8 injection payloads)
- `test_meter_step_is_order_independent`
- `test_turn_survives_total_provider_outage`

Run: `cd services/engine && python -m pytest tests/ -q`

---

## The knowledge economy — how a mind actually changes

Four mechanisms, all pure engine code, no model involved in any of them.

**1. Disclosure is gated.** `P = candor + trust(room) - risk(fact)`. The model
proposes *which* belief a character reaches for; the engine decides whether it
comes out. Trust is read at the room's **minimum**, so one enemy present
silences a speaker — which is what makes the composition of a scene something
the player can manipulate. The floor is 0.02, never 0: secrets get out by slip
far more often than by decision.

**2. Talk has a ceiling.** Assertion can never produce `KNOWS` and never exceeds
0.6, however many mouths repeat it. Without this every conversation is a step
toward everyone knowing everything.

**3. Evidence is the only route to certainty.** A physical object at a location,
never projected — *found*, not perceived. This inverts the economics: leaks can
be generous, because a scheme that advances loudly leaves more trail instead of
solving itself.

**4. Contradiction, so a mind can be changed rather than only added to.** Facts
declare what they are incompatible with. `truthful: false` asserts that
contradiction — Ollivar, who knows the king was poisoned, says he died of age,
and the listener acquires that *specific false belief*. The pairing is authored
seed data, so "invent a convincing lie" stays inexpressible, exactly as
"invent a fact" already was.

Receiving a claim contests whatever it contradicts, weighted on both sides by
the listener's regard for the source. **Something seen first-hand carries full
credibility (1.0); hearsay from someone despised carries a third of it.** The
incoming claim can lose, and losing shakes a belief rather than erasing it —
people get uncertain long before they change their minds.

The engine never reads `is_true` to do any of this. A false belief defends
itself exactly as well as a true one.


---

## Just-in-time truth

Worldgen cannot enumerate everything. When the player asks about a gap, the
engine establishes the answer as a **real fact** before anyone speaks — with a
real `is_true`, held by whoever would plausibly know it, plus the plausible lie
about it. See `lore.py`.

The alternative is the failure that makes LLM roleplay feel like sand: a model
invents a number inside its prose, that number exists only in a transcript,
nobody else in the world knows it, and the next character asked contradicts it.

`world.lore_index` makes it **idempotent by topic** — asking three characters
the same question interrogates one truth instead of minting three.

### Facts that events can move, and the lock on the rest

Immutable is the **default**. "The king was poisoned" is true forever or was
never true. A few facts are standing quantities — "the Watch numbers 2,100 men"
— which are true today and which a battle should change.

`mutable` is decided **at creation and never afterwards**, and no model-facing
path can set it. That one-way door is the whole safety property: an LLM able to
amend arbitrary facts would eventually amend the murder it committed, and it
would look like a legitimate state update. `locked` is a second gate for pinning
load-bearing numbers even when they are nominally quantities.

Amendment marks existing knowledge **stale rather than updated**. Silently
updating every holder would hand the world free omniscience every time a number
moved — a commander two hundred miles away should not learn his own casualties
by magic. Confidence halves, stance drops to `SUSPECTS`, and learning the new
number is a normal act of communication or discovery.

---

## Telemetry — making degradation visible

An LLM-driven simulation degrades quietly. `telemetry.py` writes one row per
model call to SQLite, grouped by `game_id` and ordered by `(turn, seq)` so a
session reads back chronologically.

The column that matters is `violations`. The engine **already** forgives an
out-of-range belief index, an unparseable `hours_elapsed`, an unknown action —
that forgiveness keeps a turn alive, and it is also exactly what makes drift
invisible. A model hallucinating belief indices looks identical to a quiet NPC.
Every silent repair is now counted and named.

- `GET /games/{id}/telemetry` — the chronological log
- `GET /telemetry/health` — breakdowns by provider, call kind, and prompt-length
  band. Breakdowns rather than one score, because a quality drop is almost
  always localised and an average is what hides it.

The suite has an autouse guard that fails loudly on any attempt to reach a
model, so a test can never pass or fail for network reasons.
