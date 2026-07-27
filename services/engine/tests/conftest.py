"""Shared test guards.

The suite must never touch the network. That is not just about speed: a test
that silently reaches a live provider passes or fails for reasons that have
nothing to do with the code, which is the worst possible property for the
deterministic half of an LLM project.

`_no_network` is autouse, so a new test calling into an unstubbed model path
fails loudly with "the test suite tried to reach a model" instead of hanging on
a connection timeout and looking like a slow test.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError(
            "the test suite tried to reach a model — stub llm.complete_json / "
            "complete_text, or pass establish_lore=False")

    # Patch the transport, not the callers: every prompt path in llm.py funnels
    # through these two, so nothing can slip past by taking a different route.
    monkeypatch.setattr("app.llm._post", forbidden, raising=False)
    yield
