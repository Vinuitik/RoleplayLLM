# The Hand of the King

A political intrigue RP simulator. You are Orys Ashwood, nine days into the
office of Hand. The king is dying and everyone at the table is lying about
something different.

The premise the whole build serves: **the world moves whether or not you act**,
and **nobody — including the narrator — knows what is true except the engine.**

> Read [FLOWS.md](FLOWS.md) for how it works and why. This file is just how to
> run it.

---

## Run it

### 1. Start the host-wrapper (on the host, not in Docker)

```bash
cd host-wrapper
./start.bat          # or: pip install -r requirements.txt && python main.py
```

It must run on the host because it shells out to the `claude` CLI so Claude calls
bill your **subscription** rather than metered API tokens. Containerising it
would silently move everything onto paid tokens.

Check it: <http://127.0.0.1:5501/providers>

### 2. Start the stack

```bash
docker compose up -d
```

Open <http://127.0.0.1:8091>.

### 3. Optional — play on your phone

```bash
docker compose --profile tunnel up -d
docker compose logs cloudflared | grep trycloudflare.com
```

That prints a free `https://<words>.trycloudflare.com` URL — no domain, no
account. Open it on the phone and **Add to Home Screen** to install the PWA. The
TLS is what makes the service worker (and therefore installation) work at all.

The URL changes on every restart. For a stable one, set
`CLOUDFLARE_TUNNEL_TOKEN` in `.env` and switch the compose command to
`tunnel run --token ...`.

---

## Offline play

Install [Ollama](https://ollama.com) on the host, then:

```bash
ollama pull llama3.1:8b
# Windows: setx OLLAMA_HOST 0.0.0.0   (so the phone can reach it over the LAN)
```

`ollama` sits last on both provider chains, so it is only reached when every
hosted provider is unreachable — exactly the offline case. Nothing to toggle.

Running Ollama *on the phone itself* is out of scope: a 7B model on a handset is
technically possible and genuinely miserable. The phone uses the LAN or the APIs.

---

## Development

```bash
# tests — 84, deterministic, no LLM calls, ~1s
cd services/engine && python -m pytest tests/ -q

# engine alone
WRAPPER_URL=http://127.0.0.1:5501 DATABASE_URL=sqlite:///./rp.db \
  python -m uvicorn app.main:app --reload --port 8090

# frontend (needs node; not installed on this host — Docker builds it instead)
cd frontend && npm install && npm run dev
```

### Layout

```
host-wrapper/        Flask + provider router. Runs on the HOST.
services/engine/     FastAPI + the simulation. All logic lives here.
  app/models.py        state vocabulary (Fact / Belief join table / Meter / …)
  app/projection.py    the wall — what a model is allowed to see
  app/turn.py          the loop
  app/world/seed.json  the world, hand-authored
frontend/            React (atomic design) + nginx + PWA
```

Frontend follows atomic design — `atoms → molecules → organisms → pages`, imports
only ever flow upward, and only `pages/` fetches.

---

## Endpoints worth knowing

| Endpoint | What |
|---|---|
| `POST /games` | new game |
| `POST /games/{id}/turn` | take a turn |
| `POST /games/{id}/rewind/{turn}` | restore an earlier turn (destructive) |
| `GET  /games/{id}/dm` | **full unfiltered state beside every character's projection** — the tool for proving hidden state never leaks |
| `GET  /games/{id}/truth` | post-game: what was true vs what you believed, and who lied to you |

---

## Configuration

Everything lives in `.env` (gitignored — it holds real keys copied from
ObsidianOptimizer).

| Variable | Default | Notes |
|---|---|---|
| `PORT` | `5501` | wrapper port; clear of ObsidianOptimizer's 5500 |
| `WRAPPER_URL` | `http://host.docker.internal:5501` | how containers reach the host |
| `LLM_TEXT_PRIORITY` | free tiers first | NPC intentions — cheap, schema-constrained |
| `LLM_NARRATOR_PRIORITY` | `claude-cli` first | narration is the one call you read |
| `DATABASE_URL` | `sqlite:////data/rp.db` | Postgres is a URL change; schema is portable |
| `RP_SEED` | blank | fixed seed = same dice every playthrough |

---

## Claude subscription

Claude Code is installed natively at `~/.local/bin/claude.exe`, authenticated by
OAuth on a **`pro`** subscription. `ANTHROPIC_API_KEY` is left blank on purpose:
with no key, the router can only reach Claude through the CLI, so Claude usage
can never silently fall onto metered tokens.

If the CLI stops resolving (a reinstall moves it, PATH goes stale), set
`CLAUDE_BIN=/full/path/to/claude.exe` in `.env` — the router checks that first.

Verify anytime:

```bash
curl -s -X POST http://127.0.0.1:5501/complete \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Reply with exactly: ok","capability":"narrate"}'
# expect: {"provider":"claude-cli","text":"ok"}
```

## Known gaps

- **NPCs have no reluctance.** A conspirator will volunteer the conspiracy the
  first time you talk to them. Trust/evidence/hesitation is designed-but-unbuilt —
  see the last section of [FLOWS.md](FLOWS.md).
- **Node is not installed on this host**, so the frontend only builds inside
  Docker. `npm run dev` needs Node installed first.
