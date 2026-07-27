"""LLM router — one gateway for every model provider the app talks to.

Free-tier providers are tried in priority order; Claude (API key or CLI
subscription credits) is the LAST resort so coding/subscription quota is
never burned on bulk image or text work while a free provider still has
quota left.

Concurrency model — queue sharding, not racing: each in-flight request
leases a different provider (in_flight cap of 1), so when the Java worker
sends several images concurrently they fan out across free quotas
(image A → Gemini while image B → Groq). A provider is also rate-spaced
(min_interval between request starts, derived from its free-tier RPM) and
benched on 429/5xx with an escalating cooldown that honors Retry-After.

All keys come from the repo-root .env (single source of truth) — see
.env.example. A provider with no key is simply skipped.
"""

import base64
import json
import os
import shutil
import subprocess
import threading
import time

import requests

REQUEST_TIMEOUT_S = int(os.environ.get("LLM_REQUEST_TIMEOUT_S", "120"))
ACQUIRE_DEADLINE_S = int(os.environ.get("LLM_ACQUIRE_DEADLINE_S", "150"))
TEXT_MAX_TOKENS = int(os.environ.get("LLM_TEXT_MAX_TOKENS", "4096"))
VISION_MAX_TOKENS = int(os.environ.get("LLM_VISION_MAX_TOKENS", "1024"))
CLI_TIMEOUT_S = int(os.environ.get("CLI_TIMEOUT_S", "180"))


def _parse_batch_limits():
    """LLM_VISION_BATCH=gemini:4,github:2,... — images per request a provider
    handles without quality collapse or 429s (calibrated 2026-06-13, see
    host-wrapper/calibration/). Unlisted providers default to 1 (no batching)."""
    raw = os.environ.get("LLM_VISION_BATCH", "")
    limits = {}
    for part in raw.split(","):
        if ":" in part:
            name, _, n = part.strip().partition(":")
            try:
                limits[name.strip()] = max(1, int(n))
            except ValueError:
                pass
    return limits


VISION_BATCH_LIMITS = _parse_batch_limits()


# Request priority for the shared, scarce LLM providers (lower = more important).
# Image-captions/flashcards/ingest-synthesis all draw from the SAME free-tier
# providers, so when tokens are scarce a freed provider goes to the highest-priority
# waiter first (ingest > flashcards > image-captions). See _acquire.
PRIORITY = {"high": 0, "medium": 1, "low": 2}
DEFAULT_PRIORITY = "medium"


class RouterError(Exception):
    """All candidate providers failed or none are configured."""


class _RateLimited(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


class Provider:
    """One upstream. kind: 'openai' (OpenAI-compatible HTTP), 'anthropic', 'cli'."""

    def __init__(self, name, kind, key, url=None,
                 text_model=None, vision_model=None, min_interval=1.0,
                 no_auth=False):
        self.name = name
        self.kind = kind
        self.key = (key or "").strip()
        self.url = url
        self.text_model = text_model
        self.vision_model = vision_model
        self.min_interval = min_interval
        # Local providers (ollama) speak the OpenAI wire format but take no
        # credential — without this they'd read as unconfigured and be skipped.
        self.no_auth = no_auth
        self.vision_batch = VISION_BATCH_LIMITS.get(name, 1)
        # mutable state, guarded by Router._lock
        self.in_flight = 0
        self.next_start = 0.0
        self.cooldown_until = 0.0
        self.consecutive_failures = 0
        self.ok_count = 0
        self.fail_count = 0

    @property
    def configured(self):
        return self.kind == "cli" or self.no_auth or bool(self.key)

    def supports(self, capability):
        # 'narrate' and 'orchestrate' are text work on different priority chains,
        # not different model slots — they resolve against text_model like plain
        # text does. Only vision needs a distinct model.
        model = self.vision_model if capability == "vision" else self.text_model
        return model is not None

    def bench(self, seconds=None):
        self.consecutive_failures += 1
        if seconds is None:
            # No Retry-After header → transient (usually a per-second rate cap that
            # clears in ~1s). Recover fast: floor 1s, exponential on CONSECUTIVE
            # failures (1,2,4,8,…), capped 3600s so a genuinely-down provider still
            # backs off. A provider-supplied Retry-After (seconds set) is honored
            # verbatim/uncapped instead — e.g. a daily-cap provider's multi-hour wait.
            seconds = min(2 ** (self.consecutive_failures - 1), 3600)
        self.cooldown_until = time.time() + seconds
        self.fail_count += 1

    def succeed(self):
        self.consecutive_failures = 0
        self.ok_count += 1


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _build_providers():
    """Free tiers first. min_interval ≈ 60 / free-tier RPM."""
    return {p.name: p for p in [
        Provider("gemini", "openai", _env("GEMINI_API_KEY"),
                 url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                 text_model=_env("GEMINI_MODEL", "gemini-2.5-flash"),
                 vision_model=_env("GEMINI_MODEL", "gemini-2.5-flash"),
                 min_interval=4.0),    # 15 RPM free tier
        Provider("github", "openai",
                 _env("GITHUB_MODELS_TOKEN") or _env("GITHUB_TOKEN"),
                 url="https://models.github.ai/inference/chat/completions",
                 text_model=_env("GITHUB_MODELS_MODEL", "openai/gpt-4o-mini"),
                 vision_model=_env("GITHUB_MODELS_MODEL", "openai/gpt-4o-mini"),
                 min_interval=4.0),    # ~15 RPM free tier
        Provider("mistral", "openai", _env("MISTRAL_API_KEY"),
                 url="https://api.mistral.ai/v1/chat/completions",
                 text_model=_env("MISTRAL_MODEL", "mistral-small-latest"),
                 vision_model=_env("MISTRAL_MODEL", "mistral-small-latest"),
                 min_interval=1.5),    # 1 RPS free tier
        Provider("groq", "openai", _env("GROQ_API_KEY"),
                 url="https://api.groq.com/openai/v1/chat/completions",
                 text_model=_env("GROQ_TEXT_MODEL", "llama-3.3-70b-versatile"),
                 # Groq retired every vision-capable Llama model (llama-4-scout/maverick,
                 # llama-3.2-*-vision) — confirmed 2026-07-23 via GET /openai/v1/models,
                 # none of the models on this key take image input anymore. No default
                 # here (was a 404 model_not_found on every vision call); set
                 # GROQ_VISION_MODEL in .env if Groq ships a vision model again.
                 vision_model=_env("GROQ_VISION_MODEL") or None,
                 min_interval=2.0),    # 30 RPM free tier
        Provider("deepseek", "openai", _env("DEEPSEEK_API_KEY"),
                 url="https://api.deepseek.com/chat/completions",
                 text_model=_env("DEEPSEEK_MODEL", "deepseek-chat"),
                 vision_model=None,    # no vision endpoint
                 min_interval=1.0),    # paid (cheap) — no hard free limit
        Provider("anthropic", "anthropic", _env("ANTHROPIC_API_KEY"),
                 text_model=_env("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
                 vision_model=_env("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
                 min_interval=1.5),
        Provider("claude-cli", "cli", None,
                 text_model=_env("SYNTH_MODEL", "haiku"),
                 vision_model=None,
                 min_interval=1.0),
        # Local Ollama — the offline path. Never rate-limited and never metered,
        # but slow and weaker, so it sits last on every chain by default. Reached
        # over host.docker.internal because ollama runs on the host, not in compose.
        Provider("ollama", "openai", None,
                 url=_env("OLLAMA_URL", "http://host.docker.internal:11434")
                     .rstrip("/") + "/v1/chat/completions",
                 text_model=_env("OLLAMA_MODEL", "llama3.1:8b"),
                 vision_model=None,
                 min_interval=0.0,   # local box, no quota to protect
                 no_auth=True),
    ]}


def _priority(env_name, default):
    raw = _env(env_name) or default
    return [n.strip() for n in raw.split(",") if n.strip()]


# Vision: Gemini first (1500 req/day, native vision). Claude API dead last.
# groq excluded — no vision-capable model left on the free tier (see _build_providers).
VISION_PRIORITY = _priority("LLM_VISION_PRIORITY",
                            "gemini,github,mistral,anthropic")
# Text = NPC intentions: short, schema-constrained JSON where model quality barely
# shows. Fastest free tiers first; subscription credits only when they're spent.
TEXT_PRIORITY = _priority("LLM_TEXT_PRIORITY",
                          "groq,github,mistral,deepseek,gemini,claude-cli,ollama")
# Narration is the ONE call the player actually reads, and it's once per turn —
# so it gets the good models first. Inverted relative to TEXT_PRIORITY on purpose.
NARRATE_PRIORITY = _priority("LLM_NARRATOR_PRIORITY",
                             "claude-cli,gemini,mistral,groq,github,ollama")
# Orchestration = the planner: sees the whole world, emits plans, never addresses
# the player. Reasoning quality is the entire point and it runs at most once a
# turn, so claude-cli (subscription, not metered) leads and there is no cheap
# fallback worth taking before gemini. A failed orchestrator call must degrade to
# "no plan this turn", never to a bad plan.
ORCHESTRATE_PRIORITY = _priority("LLM_ORCHESTRATOR_PRIORITY",
                                 "claude-cli,gemini,mistral,github,groq,ollama")


class Router:
    def __init__(self):
        self.providers = _build_providers()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        # Priority levels of requests currently BLOCKED in _acquire (a multiset).
        # A waiter only takes an available provider if no strictly-higher-priority
        # request is also waiting — so ingest beats flashcards beats image-captions
        # for a freed token. Guarded by _lock.
        self._waiting = []  # list[int] priority values of blocked acquirers

    # ── leasing ──────────────────────────────────────────────────────────

    def _chain(self, capability):
        order = {"vision": VISION_PRIORITY,
                 "narrate": NARRATE_PRIORITY,
                 "orchestrate": ORCHESTRATE_PRIORITY}.get(capability, TEXT_PRIORITY)
        return [self.providers[n] for n in order
                if n in self.providers
                and self.providers[n].configured
                and self.providers[n].supports(capability)]

    def _acquire(self, capability, skip, priority=DEFAULT_PRIORITY):
        """Lease the best free provider; block until one frees up. When several
        requests wait, a freed provider goes to the highest-priority waiter first
        (ingest 'high' > flashcards 'medium' > image-captions 'low'): a waiter only
        TAKES an available provider if no strictly-higher-priority request is also
        blocked, else it yields a beat so the important one wins. Providers overlap
        across chains, so the yield is capability-agnostic on purpose."""
        prio = PRIORITY.get(priority, PRIORITY[DEFAULT_PRIORITY])
        deadline = time.time() + ACQUIRE_DEADLINE_S
        with self._cv:
            self._waiting.append(prio)
            try:
                return self._acquire_loop(capability, skip, prio, deadline)
            finally:
                self._waiting.remove(prio)

    def _acquire_loop(self, capability, skip, prio, deadline):
        """The blocking lease loop. Runs with self._cv held (called from _acquire)."""
        while True:
            chain = [p for p in self._chain(capability) if p.name not in skip]
            if not chain:
                raise RouterError(
                    f"no providers left for '{capability}' "
                    f"(configured: {[p.name for p in self._chain(capability)]}, "
                    f"failed this request: {sorted(skip)})")
            now = time.time()
            # A strictly-higher-priority request is also blocked → let it have the
            # next freed provider; wait a beat instead of grabbing one now.
            higher_waiting = any(w < prio for w in self._waiting)
            if not higher_waiting:
                for p in chain:
                    if p.cooldown_until > now or p.in_flight >= 1 or p.next_start > now:
                        continue
                    p.in_flight += 1
                    p.next_start = now + p.min_interval
                    return p
            if now >= deadline:
                raise RouterError(
                    f"all '{capability}' providers busy/cooling for "
                    f"{ACQUIRE_DEADLINE_S}s: "
                    + ", ".join(f"{p.name}(cooldown {max(0, int(p.cooldown_until - now))}s,"
                                f" in_flight {p.in_flight})" for p in chain))
            # Fail fast when provably doomed: no candidate is in flight (so no
            # release can free one early) and every candidate's earliest
            # availability lies beyond the deadline. Without this, a request
            # arriving while all providers sit on long Retry-After benches
            # (e.g. daily caps) blocks the caller the full ACQUIRE_DEADLINE_S
            # before failing — pure dead air.
            if not any(p.in_flight for p in chain):
                soonest = min(max(p.cooldown_until, p.next_start) for p in chain)
                if soonest > deadline:
                    raise RouterError(
                        f"all '{capability}' providers cooling past the "
                        f"{ACQUIRE_DEADLINE_S}s acquire deadline (soonest in "
                        f"{int(soonest - now)}s): "
                        + ", ".join(f"{p.name}(cooldown {max(0, int(p.cooldown_until - now))}s)"
                                    for p in chain))
            self._cv.wait(timeout=0.25)

    def _release(self, provider, ok, retry_after=None):
        with self._cv:
            provider.in_flight -= 1
            if ok:
                provider.succeed()
            else:
                provider.bench(retry_after)
            self._cv.notify_all()

    # ── public API ───────────────────────────────────────────────────────

    def complete_text(self, prompt, system=None, cli_model=None,
                      priority=DEFAULT_PRIORITY, capability="text"):
        """Returns (text, provider_name). Raises RouterError when exhausted.

        capability picks the priority chain: 'text' (cheap-first, for NPC
        intentions) or 'narrate' (quality-first, for the prose the player reads).
        """
        return self._run(capability,
                         lambda p: self._call_text(p, prompt, system, cli_model),
                         priority=priority)

    def complete_json(self, prompt, system=None, cli_model=None,
                      priority=DEFAULT_PRIORITY, capability="text",
                      required_keys=(), attempts=3):
        """Text completion constrained to ONE JSON object. Returns (obj, provider).

        The engine treats every LLM reply as an untrusted proposal, so this is
        deliberately paranoid: json_mode is only a nudge, `_parse_json_object`
        digs the object out of prose/fences, and required_keys is checked before
        the value is handed back. A reply that fails any of that is retried with
        an explicit repair instruction; each retry re-enters the router, so a
        provider that keeps babbling prose gets skipped for one that doesn't.
        """
        last = None
        for attempt in range(attempts):
            instruction = prompt if attempt == 0 else (
                f"{prompt}\n\nYour previous reply was not valid JSON matching the "
                f"required shape ({last}). Reply with ONE JSON object and nothing "
                f"else — no prose, no markdown fences.")
            text, provider = self._run(
                capability,
                lambda p: self._call_text(p, instruction, system, cli_model,
                                          json_mode=True),
                priority=priority)
            obj = _parse_json_object(text)
            if obj is None:
                last = "not parseable as a JSON object"
                continue
            missing = [k for k in required_keys if k not in obj]
            if missing:
                last = f"missing required keys: {missing}"
                continue
            return obj, provider
        raise RouterError(f"no provider returned valid JSON after {attempts} "
                          f"attempts (last failure: {last})")

    def complete_vision(self, prompt, image_bytes, media_type, priority=DEFAULT_PRIORITY):
        """Returns (text, provider_name). Raises RouterError when exhausted."""
        b64 = base64.standard_b64encode(image_bytes).decode()
        return self._run("vision", lambda p: self._call_vision(p, prompt, b64, media_type),
                         priority=priority)

    def complete_vision_batch(self, single_prompt, batch_prompt_tmpl, images,
                              priority=DEFAULT_PRIORITY):
        """images: list of (bytes, media_type). Returns (list[str], provider).

        One provider lease serves the whole list, split into that provider's
        calibrated sub-batch size (vision_batch). A sub-batch whose JSON-array
        reply doesn't keep the images separate falls back to per-image calls
        on the same provider — callers always get len(images) results.
        """
        encoded = [(base64.standard_b64encode(b).decode(), mt) for b, mt in images]
        return self._run("vision", lambda p: self._call_vision_batch(
            p, single_prompt, batch_prompt_tmpl, encoded), priority=priority)

    def _run(self, capability, call, priority=DEFAULT_PRIORITY):
        skip = set()
        errors = []
        while True:
            try:
                provider = self._acquire(capability, skip, priority)
            except RouterError as e:
                if errors:
                    raise RouterError(f"{e} — failures: {'; '.join(errors)}") from None
                raise
            try:
                text = call(provider)
            except _RateLimited as e:
                self._release(provider, ok=False, retry_after=e.retry_after)
                skip.add(provider.name)
                errors.append(f"{provider.name}: 429 (cooling "
                              f"{e.retry_after or 'default'}s)")
                continue
            except Exception as e:
                self._release(provider, ok=False)
                skip.add(provider.name)
                errors.append(f"{provider.name}: {e}")
                continue
            self._release(provider, ok=True)
            return text, provider.name

    # ── per-kind calls ───────────────────────────────────────────────────

    def _call_text(self, p, prompt, system, cli_model, json_mode=False):
        if p.kind == "cli":
            return _claude_cli(prompt, system, cli_model or p.text_model)
        if p.kind == "anthropic":
            return _anthropic_messages(p, [{"role": "user", "content": prompt}],
                                       system=system, max_tokens=TEXT_MAX_TOKENS)
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        return _openai_chat(p, p.text_model, messages, TEXT_MAX_TOKENS,
                            json_mode=json_mode)

    def _call_vision(self, p, prompt, b64, media_type):
        return self._vision_request(p, prompt, [(b64, media_type)], VISION_MAX_TOKENS)

    def _vision_request(self, p, prompt, encoded, max_tokens):
        """One HTTP request carrying 1..N images."""
        if p.kind == "anthropic":
            content = [{"type": "image",
                        "source": {"type": "base64", "media_type": mt, "data": b64}}
                       for b64, mt in encoded]
            content.append({"type": "text", "text": prompt})
            return _anthropic_messages(p, [{"role": "user", "content": content}],
                                       max_tokens=max_tokens)
        parts = [{"type": "text", "text": prompt}]
        parts += [{"type": "image_url",
                   "image_url": {"url": f"data:{mt};base64,{b64}"}}
                  for b64, mt in encoded]
        return _openai_chat(p, p.vision_model,
                            [{"role": "user", "content": parts}], max_tokens)

    def _call_vision_batch(self, p, single_prompt, batch_tmpl, encoded):
        limit = max(1, p.vision_batch)
        out = []
        for i in range(0, len(encoded), limit):
            sub = encoded[i:i + limit]
            if i > 0:
                time.sleep(p.min_interval)   # pace sub-requests within one lease
            if len(sub) == 1:
                out.append(self._vision_request(p, single_prompt, sub,
                                                VISION_MAX_TOKENS))
                continue
            text = self._vision_request(p, batch_tmpl.format(n=len(sub)), sub,
                                        VISION_MAX_TOKENS * len(sub))
            arr = _parse_json_array(text)
            if arr is not None and len(arr) == len(sub):
                out.extend(str(a) for a in arr)
            else:
                # model merged the images — degrade to per-image calls here
                for j, one in enumerate(sub):
                    if j > 0:
                        time.sleep(p.min_interval)
                    out.append(self._vision_request(p, single_prompt, [one],
                                                    VISION_MAX_TOKENS))
        return out

    # ── introspection (dashboard / debugging) ────────────────────────────

    def status(self):
        now = time.time()
        with self._lock:
            return {p.name: {
                "configured": p.configured,
                "in_flight": p.in_flight,
                "cooldown_s": max(0, int(p.cooldown_until - now)),
                "ok": p.ok_count,
                "failed": p.fail_count,
            } for p in self.providers.values()}


def _openai_chat(p, model, messages, max_tokens, json_mode=False):
    headers = {"Content-Type": "application/json"}
    if not p.no_auth:
        headers["Authorization"] = f"Bearer {p.key}"
    body = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if json_mode:
        # Supported by OpenAI-compatible endpoints incl. groq/mistral/gemini and
        # ollama. Providers that ignore it still usually comply from the prompt,
        # and the caller validates the payload regardless — so this is a nudge,
        # never the guarantee.
        body["response_format"] = {"type": "json_object"}
    resp = requests.post(p.url, headers=headers, json=body,
                         timeout=REQUEST_TIMEOUT_S)
    if resp.status_code == 429:
        raise _RateLimited(_retry_after(resp))
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"]


def _parse_json_array(text):
    """Best-effort: strip code fences, find the outermost JSON array."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    try:
        out = json.loads(t)
        return out if isinstance(out, list) else None
    except json.JSONDecodeError:
        lo, hi = t.find("["), t.rfind("]")
        if 0 <= lo < hi:
            try:
                out = json.loads(t[lo:hi + 1])
                return out if isinstance(out, list) else None
            except json.JSONDecodeError:
                return None
    return None


def _parse_json_object(text):
    """Best-effort: strip code fences, find the outermost JSON object.

    Object-shaped twin of _parse_json_array. Models routinely wrap JSON in
    ```json fences or a sentence of preamble, and on the free tiers we cannot
    rely on response_format being honoured — so we dig rather than reject.
    """
    t = (text or "").strip()
    if t.startswith("```"):
        parts = t.split("```")
        if len(parts) > 1:
            t = parts[1]
            if t.startswith("json"):
                t = t[4:]
            t = t.strip()
    try:
        out = json.loads(t)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        lo, hi = t.find("{"), t.rfind("}")
        if 0 <= lo < hi:
            try:
                out = json.loads(t[lo:hi + 1])
                return out if isinstance(out, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def _retry_after(resp):
    try:
        return max(1, int(float(resp.headers.get("Retry-After"))))
    except (TypeError, ValueError):
        return None


def _anthropic_messages(p, messages, system=None, max_tokens=1024):
    import anthropic
    client = anthropic.Anthropic(api_key=p.key)
    kwargs = {"model": p.text_model, "max_tokens": max_tokens, "messages": messages}
    if system:
        kwargs["system"] = system
    try:
        message = client.messages.create(**kwargs)
    except anthropic.RateLimitError:
        raise _RateLimited(None)
    return message.content[0].text


def _find_claude():
    """Locate the claude CLI without depending on PATH being current.

    PATH alone is unreliable here: the native Windows installer drops the binary
    in ~/.local/bin and tells you to restart your terminal, so any process
    started before that (like a long-running wrapper) never sees it. Since this
    provider is the whole reason Claude calls bill the subscription instead of
    metered tokens, failing to find the binary silently moves spend onto other
    providers — worth being thorough about.

    Order: explicit override, then PATH, then the known install locations.
    """
    override = os.environ.get("CLAUDE_BIN", "").strip()
    if override and os.path.isfile(override):
        return override

    found = shutil.which("claude")
    if found:
        return found

    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".local", "bin", "claude.exe"),   # native win installer
        os.path.join(home, ".local", "bin", "claude"),       # native linux/mac
        os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"),  # npm shim
        os.path.join(home, ".claude", "local", "claude"),
        "/usr/local/bin/claude",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return "claude"   # let subprocess raise a clear error


def _claude_cli(prompt, system, model):
    claude_bin = _find_claude()
    cmd = [claude_bin, "-p", "--output-format", "json", "--model", model]
    if system:
        cmd += ["--append-system-prompt", system]
    result = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                            encoding="utf-8", timeout=CLI_TIMEOUT_S)
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI exit {result.returncode}: {result.stderr[:500]}")
    try:
        return json.loads(result.stdout).get("result", "")
    except json.JSONDecodeError:
        return result.stdout.strip()
