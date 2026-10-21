# 卡 A1（2026-10-21）Alpaca 期权历史数据探测 — 低档，只读
- 新增 desk/alpaca_probe.py：对 SOXX/SMH/QQQ/SPY/NVDA/AMD/MU/INTC 查已到期合约（最早到期日）、抽一年前到期的近平值看跌期权查日线 bars 起点、试历史 quotes 接口。
- 本机连不上 Alpaca（代理 403），用 MockTransport 验过解析逻辑；全部测试通过。
- 待服务器实测：venv/bin/python -m desk.alpaca_probe → data/reports/alpaca_probe.json。
