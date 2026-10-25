import os

from desk import config


def test_config_never_loads_real_env_in_tests(tmp_path):
    """服务器上跑测试时，子进程不许从真实 .env 读到 API key（曾经让 3 条测试真的去调大模型）。"""
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-should-not-load\n")
    assert os.environ.get("DESK_TEST") == "1"
    config.load_env(tmp_path / ".env")
    assert "ANTHROPIC_API_KEY" not in os.environ
