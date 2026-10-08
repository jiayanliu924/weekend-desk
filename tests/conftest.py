import pytest


@pytest.fixture(autouse=True)
def _no_real_side_effects(monkeypatch):
    """Tests must never push to the real phone channel or call the real LLM."""
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
