# HANDOFF — orchestrator architecture redesign

You are picking up a working project at an architectural fork. Read `FLOWS.md`
for mechanics; this file is about **what to decide and why the previous session
should not be trusted to decide it**.

---

## What exists and runs today

A political-intrigue RP simulator. `docker compose up -d` + `host-wrapper/start.bat`
→ http://127.0.0.1:8091. 84 deterministic tests pass in ~1s (`cd services/engine
&& python -m pytest tests/ -q`). Verified end to end against live providers.

```
browser → nginx :8091 ─┬─ /      React SPA (atomic design) + PWA
                       └─ /api/  → engine :8090 (docker, FastAPI)
                                       └→ host-wrapper :5501 (HOST, not docker)
                                              └→ groq│github│mistral│gemini│claude-cli│ollama
```

The wrapper is on the host deliberately: it shells out to the `claude` CLI so
Claude bills the **Pro subscription**, not metered tokens. `ANTHROPIC_API_KEY` is
blank on purpose so it cannot silently fall onto paid tokens. Do not containerise it.

---

## The fork

The user wants to move from a **fixed turn loop** to an **LLM orchestrator with a
toolbox** — a strong model (Opus via claude-cli) planning scenes and adjusting
dynamically, cheaper models voicing individual characters. Motivation, in their
words: *"court is limiting, maybe I want to resolve a battle, maybe something else
entirely. You can't easily put a boundary."*

They also want **genuine group conversation** — turn-by-turn relay through a room
of participants — and accept it may be expensive.

They explicitly asked whether the previous session had become biased toward what
it had already built. It answered: **yes, partly.** Discount the previous
session's opinions on the turn loop, action vocabulary, and scene model. Those are
exactly the pieces under question and the ones it was slowest to abandon.

---

## The distinction that matters most

The previous session over-built the *vocabulary* and under-defended the *epistemics*.
Keep these separate when you redesign.

**Keep rigid — this is only one invariant:**

> A fact has holders. A belief is an edge. Projection filters by edge.

Nothing in that says what a fact is *about*. An orchestrator can invent facts,
entities, meters, locations, whole scenarios — battles, plagues, markets — and
projection still works, because it never inspects content. **New content is free.
Fluid epistemics are not.**

**Should become fluid / LLM-driven:**
- the action vocabulary (currently a hardcoded dict in `gametime.py` +
  `_contest()` in `turn.py`)
- scene structure (currently: at most one hardcoded NPC *pair* per turn)
- the fixed 7-step turn pipeline itself

---

## Known bugs and gaps — fix regardless of architecture

1. **Speech only reaches its target.** `turn.py::_speak` propagates the belief to
   the addressed character only. Someone standing in the same room hears nothing.
   Speech should propagate to **every witness at the location**; `target` should
   shape prose only. This is a genuine bug, ~20 lines, and it makes group
   conversation mostly fall out for free (two people in a room is just a scene
   with two participants).

2. **NPCs have no reluctance.** Reproduced on the first live turn: Mycella Ferrow,
   a *conspirator*, volunteered the entire Dragonstone conspiracy to the player at
   0.9 confidence, unprompted. Nothing gates disclosure.

   Agreed design (not built):
   ```
   P(disclose) = base(candor_trait)
               + trust(speaker → listener)   # make `relationships` a mutable float
               - risk(fact, speaker)         # in `hides`? plot member? tagged secret?
   ```
   Roll it. Applies identically NPC→NPC and NPC→player — no special case for the
   player. **The LLM proposes intent to disclose; the engine gates it.**

3. **Talk should have a confidence ceiling.** Proposed and agreed but unbuilt:
   assertion alone can never set `stance=KNOWS` or exceed ~0.6 confidence. Only
   **evidence** (a proposed first-class object: `{fact_id, kind, location, holder,
   strength}`) creates certainty. This is what structurally prevents the court
   collapsing into omniscient opponents, and it lets clues stay findable instead
   of needing to be punishingly rare.

4. **Plot exposure spawns suspicion from nowhere.** It should *drop evidence at a
   location* the player can find, turning a conspiracy into a physical trail.

---

## Corrections the previous session made to its own advice

- **An orchestrator seeing everything is FINE.** The session initially said a
  director re-introduces omniscience; that was wrong. Omniscience is acceptable in
  any component that never addresses the player directly. The constraint belongs
  on the **narrator**, not the planner.

- **Offscreen scenes should cost zero tokens.** Meetings the player cannot
  perceive need *state changes*, not prose: run the trust roll, disclosure roll and
  belief propagation mechanically, and only generate writing if the player later
  learns of it. This is where nearly all the cost saving is, and it is what makes
  genuine group chat affordable for scenes that matter.

---

## Non-negotiables — verify any redesign preserves these

These are load-bearing and each has tests. Breaking one is fine *if deliberate*;
breaking one by accident ruins the game silently.

1. **Secrets by absence.** No prompt says "don't reveal X"; the model never
   receives X. `project(world, char_id)` builds the only view any model sees.
2. **Truth-blindness extends to the narrator.** `is_true` never crosses the
   projection boundary — not even for narration. A narrator that knows a belief is
   false hedges ("you *think* he died of old age"), and that hedge tells the player
   there is something to doubt while revealing no fact. *Leak by tone.*
3. **Engine output is filtered too.** `visible_events()` sits between resolution
   and narration. Without it the narrator faithfully reports a poisoning three
   rooms away.
4. **Fact IDs never reach a prompt.** They are authored and descriptive
   (`f_king_poisoned`). `ProjectedBelief.fact_id` is `Field(exclude=True)`.
5. **Dice decide, never the model.** The LLM proposes intent and may set
   difficulty; `resolution.py` returns the verdict.
6. **Never `eval()` an LLM-authored formula.** `formula.py` is an AST whitelist.
   8 injection payloads are tested.
7. **Time cannot freeze.** `hours_elapsed` is required, clamped `[0.05, 72]`, with
   per-archetype defaults. A model returning `0` gets the floor.
8. **Belief-index protocol.** Speakers return an *index* into their own belief
   list, never content — inventing or leaking a fact is inexpressible, not
   forbidden. Preserve this property under any new scene design.

---

## What to keep (~700 lines, scenario-agnostic, tested)

| Module | Why it survives a redesign |
|---|---|
| `models.py` | entities, belief edges, meters — nothing court-specific |
| `projection.py` | per-agent partial observability |
| `formula.py` | safe evaluator for LLM-authored math |
| `numerics.py` | any number evolving by elapsed time; idempotent, order-independent |
| `resolution.py` | opposed rolls, 5 outcome degrees, seeded |
| `gametime.py` | elapsed time, phase of day, hour coercion |
| `store.py` | per-turn snapshots, rewind, truth report |

## What is genuinely up for redesign

| Module | Problem |
|---|---|
| `turn.py` | fixed 7-step pipeline; dyad-only conversation; hardcoded action→stat map |
| `llm.py` | prompts assume dialogue scenes; no orchestrator role |
| `gametime.py::ARCHETYPE_HOURS` | action vocabulary as code, should be data |

---

## Suggested first questions to the user

1. Should the orchestrator emit a **plan spanning several turns**, or re-plan each
   turn? (Cost and coherence trade directly against each other.)
2. What is in the orchestrator's **toolbox**? Candidates: call a scene, move a
   character, advance a plot, spawn a fact, spawn evidence, set a meter formula,
   apply a modifier, end the scenario.
3. Does the player's own action get **refereed by the orchestrator** (unified) or
   stay a separate referee call (current)?
4. Should scenarios be swappable as data (`seed.json` + `actions.json`), or does
   the orchestrator generate the world too?

The user prefers being asked about big decisions and left alone for small ones.
They dislike being over-consulted on defaults. They are technically strong; do not
over-explain basics, and give recommendations rather than option surveys.
