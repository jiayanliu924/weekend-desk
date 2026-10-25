import pytest


@pytest.fixture(autouse=True)
def _no_real_side_effects(monkeypatch):
    """Tests must never push to the real phone channel or call the real LLM."""
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DESK_TEST", "1")                 # 子进程也不读真实 .env（见 config.load_env）


def as_v1(cfg_path):
    """把拷贝出来的 config.toml 切回 [strategy] version = "v1"（原 H1 规则），给写在 v2 之前、按 v1 行为断言的测试用。"""
    from pathlib import Path
    p = Path(cfg_path)
    txt = p.read_text()
    assert 'version = "v2"' in txt
    p.write_text(txt.replace('version = "v2"', 'version = "v1"', 1))


EXPANSION_MARK = "# 2026-10-15 扩展"


def core7(cfg_path):
    """把拷贝出来的 config.toml 去掉 2026-10-15 的美股单票扩展块（从「# 2026-10-15 扩展」注释行到 [clock] 之前），
    只留原来的 7 个合约。给断言写在原 7 个合约之上的测试用（模拟算术 /7、universe 扫描的 in_config 状态等）。"""
    from pathlib import Path
    p = Path(cfg_path)
    txt = p.read_text()
    a = txt.index(EXPANSION_MARK)
    b = txt.index("\n[clock]", a) + 1
    out = txt[:a].rstrip("\n") + "\n\n" + txt[b:]
    p.write_text(out)
    import tomllib
    names = [i["name"] for i in tomllib.loads(out)["market"]["instruments"]]
    assert names == ["XYZ100", "SP500", "BRENTOIL", "NVDA", "TSLA", "INTC", "SMSN"], names

