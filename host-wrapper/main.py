"""RoleplayLLM host-wrapper — the only process that talks to model providers.

Runs on the HOST, deliberately not in Docker: the router's last-resort provider
shells out to the `claude` CLI, which bills the Claude *subscription* rather than
metered API tokens. A container would have neither the CLI nor its logged-in
credentials, so this stays outside compose and the engine reaches it through
host.docker.internal (WRAPPER_URL).

Descended from ObsidianOptimizer's wrapper. The vault/image/filesystem endpoints
were dropped — an RP engine has no vault — and /complete-json was added, because
every LLM call in this app that isn't narration must come back as a validated
object the engine can act on.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# SSOT: credentials live in the repo-root .env. A host-wrapper/.env is an optional
# per-machine override (e.g. a different PORT) and wins where both define a key.
_ROOT_ENV = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ROOT_ENV)
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

from flask import Flask, request, jsonify  # noqa: E402

import llm_router  # noqa: E402  (reads env at import — keep after load_dotenv)

app = Flask(__name__)
router = llm_router.Router()


def _git_sha() -> str:
    """The commit this process was STARTED from — captured once, at import.

    Read at import rather than per-request on purpose. A per-request lookup
    would report whatever HEAD happens to be right now, which is precisely the
    lie this endpoint exists to expose: the working tree moving forward while a
    long-lived process keeps serving the code it was launched with.
    """
    import subprocess
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=here, capture_output=True,
            text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


STARTED_SHA = _git_sha()


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/version")
def version():
    """What commit is actually running here.

    The wrapper lives on the host and outlives every `docker compose up`, so it
    is the single most likely component to be quietly stale — you rebuild the
    stack, everything reports healthy, and this process is still running the
    router from three commits ago. tools/redeploy.sh asserts this matches the
    working tree, which turns "it restarted" into "it is running this commit".
    """
    return {"sha": STARTED_SHA}


@app.route("/providers")
def providers():
    """Router introspection: configured providers, cooldowns, ok/fail counts.

    Surfaced in the UI's DM panel so a stalled turn is immediately explicable
    ("everything is on a 429 bench") instead of looking like an engine bug.
    """
    return jsonify(router.status())


@app.route("/complete", methods=["POST"])
def complete():
    """Free-form text — narration only.

    Request:  {"prompt": str, "system"?: str, "model"?: str,
               "capability"?: "text"|"narrate", "priority"?: str}
    Response: {"text": str, "provider": str} | {"error": str}
    """
    data = request.json or {}
    prompt = data.get("prompt", "")
    if not prompt:
        return jsonify({"error": "prompt required"}), 422

    try:
        text, provider = router.complete_text(
            prompt,
            system=data.get("system"),
            cli_model=data.get("model"),
            priority=data.get("priority", "medium"),
            capability=data.get("capability", "text"),
        )
    except llm_router.RouterError as e:
        return jsonify({"error": str(e)}), 503

    return jsonify({"text": text, "provider": provider})


@app.route("/complete-json", methods=["POST"])
def complete_json():
    """Structured output — every non-narration call in the game goes through here.

    Request:  {"prompt": str, "system"?: str, "model"?: str,
               "required_keys"?: [str], "attempts"?: int,
               "capability"?: "text"|"narrate", "priority"?: str}
    Response: {"data": object, "provider": str} | {"error": str}

    required_keys is a shape check, not a schema: the engine re-validates the
    object against a Pydantic model on its side, because the wrapper has no
    business knowing what an NPC intention looks like.
    """
    data = request.json or {}
    prompt = data.get("prompt", "")
    if not prompt:
        return jsonify({"error": "prompt required"}), 422

    try:
        obj, provider = router.complete_json(
            prompt,
            system=data.get("system"),
            cli_model=data.get("model"),
            priority=data.get("priority", "medium"),
            capability=data.get("capability", "text"),
            required_keys=tuple(data.get("required_keys") or ()),
            attempts=int(data.get("attempts", 3)),
        )
    except llm_router.RouterError as e:
        return jsonify({"error": str(e)}), 503

    return jsonify({"data": obj, "provider": provider})


if __name__ == "__main__":
    # threaded so several NPC intentions in one turn shard across free providers
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5501)), threaded=True)
