import pytest


@pytest.fixture(autouse=True)
def _no_real_side_effects(monkeypatch):
    """Tests must never push to the real phone channel or call the real LLM."""
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def as_v1(cfg_path):
    """把拷贝出来的 config.toml 切回 [strategy] version = "v1"（原 H1 规则），给写在 v2 之前、按 v1 行为断言的测试用。"""
    from pathlib import Path
    p = Path(cfg_path)
    txt = p.read_text()
    assert 'version = "v2"' in txt
    p.write_text(txt.replace('version = "v2"', 'version = "v1"', 1))
