# 任务 A：服务器自动跑策略线并排名（完成）
- 新增 `desk/labs_auto.py`：依次跑 pairs_lab(60天/15m)、strategy_lab(含 v3)、options_real_backtest(BTC, 2023-01-01 起)，每条 try/except 互不影响；统一成一条记录并按性价比（周均/周波动）排名；写 `data/labs/latest.json` + 日期副本；推一条中文手机消息。CLI：`python -m desk.labs_auto [--offline]`。
- `desk/web.py`：新增 `/labs`「赚钱排行」页（表格 + 每列大白话解释，没跑过显示「还没跑过」），已加入导航。
- `desk/scheduler.py`：周一 13:00-17:00 UTC 的 `labs:<日期>` 任务，后台线程启动，不卡主循环。
- `update.sh`：状态行之后后台 nohup 跑一次 labs_auto。
- `desk/strategy_lab.py`：run() 加可选参数 `capture`（仅把 v3 结果交出来，算法未改）。
- 测试：`tests/test_labs_auto.py` 11 个；全量 `pytest -q tests` 312 通过。
- 假设：策略线取 v3「⑤ 走前挑选」；套利线取全额费用(taker)下性价比最高的一对，周标准差由 trades_taker 重算；期权线取卖出有限风险结构里性价比最高的组合，「杠杆」= 每周把多少比例本金当最大亏损，样本外 = 时间上靠后一半周数（近似）。
- 未决：note_optimistic 目前三条线恒为 true（杠杆都在同段历史里挑）；套利线 15m K 线只有约 7 周，几乎必然「没过门槛」；容量只有套利线有；服务器真实数据下未实跑过。
