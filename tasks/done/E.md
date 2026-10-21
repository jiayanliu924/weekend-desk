# 卡 E 完成记录（2026-10-21）
- E1 load_data 把账本里已开仓配对永远并入 data["pairs"]（held_only，不看门槛/allow，不当开仓候选），_spread_exits 因此一直有它们的 K 线。
- E2 K 线增量抓取：内存有就只抓「最后一根 t − 3 根」之后并合并、裁到回看窗口；每 6h（candle_full_refetch_h）全量；抓失败保留旧数据。
  bl.fetch_candles_paged 加 start_ms。冷启动超时 data_timeout_cold_s=300（平时 45s）。
- E3 配对标 strict（也过 [universe] 严门槛）。live 下非 strict 且不在 [pairs] loose_live_allow 的配对：不下单（_open_pair 也有最后一道闸），
  走 _shadow_scan（从 _step_shadow 抽出，共用）写独立账本 data/ledger/pairs_loose.jsonl；load_state / account_stop / 日止损都不读它。缺 strict 标记=不放行。
- E4 config max_concurrent=2，注释改为 2×$4=$8=日止损；新增 loose_live_allow=[]、data_timeout_cold_s。
- E5 live 开仓前：一手价值 > 名义×net_tolerance_frac 则不开；imbalance 平仓后也写 lock_h 锁。
- E6 pairs_gate/GATE_FLOOR 挪到 pairs_lab（pairs_exec 保留同名引用），pairs_lab.run 用它。
- 测试：python3 -m pytest -q tests/ 全过（含每条修复各 1 个新测试 test_E1..E6；两个旧超时测试补设了 data_timeout_cold_s）。
- 遗留：已开仓配对若整个从 discovery 里消失则无 K 线（仍靠实时盘口价差+halted 出场）；E5 只在 live 检查，影子记账不检查一手；
  loose 影子只记「本来会开」，没有模拟出场/盈亏；28 腿增量抓取的真实耗时要上服务器实测；冷启动期间 step 最多占 300s（flock 会让后续分钟跳过）。
- 第 2 轮复核（Sonnet）：E1/E3/E4/E5/E6 OK；E2 冷启动 6h 后所有腿同时全量重抓会撞 45s → 已修：每次最多 2 条腿全量、4 条腿刷资金费率，没刷新的保留原时间（test_E2b）。437 测试全过。
