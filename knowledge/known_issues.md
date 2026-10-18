# 已知问题清单（找 bug 的 agent 先读；报重复的直接删）
- options_real_backtest 曾偷看下单后 30 分钟成交价 → 已改为只用下单前数据（2026-10-18）
- 三份 v3/pairs/options 报告里的杠杆倍数是事后挑出来的，偏乐观（已在报告里注明）
- autotrade.exit_ 用 market_close(coin) 平全部净仓 → 手动仓会被一起平（多因子框架 #6，未修）
- growthMode 9 折手续费是补贴，决策必须用全额口径
- 7 个合约同一周末不是 7 个独立样本，显著性按日历周末聚类
- 沙盒连不上交易所；所有要拉数据的回测只能在服务器跑
- pairs_exec：PairsClient 的 mark_px/funding_rows/fill_for_cloid 只对 stub 测过，没对真交易所测过
- pairs_exec：超额敞口检查分不清手动单和漏记成交，只报警+停新单，不自动处理
- labs_auto：杠杆都是同一段历史里挑的（note_optimistic 恒为 true）；pairs 线约 7 周样本，必然没过 20 周门槛
