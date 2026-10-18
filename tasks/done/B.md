# 任务卡 B：跨 dex 同底层配对真下单（HIGH）——第 1 轮评审后
改了什么：新增 desk/pairs_exec.py（信号直接 import pairs_lab，只用已收盘 K 线）；config.toml 加 [pairs]（保守默认：每腿 $100、并发 1、总敞口 $200、单对止损 $4、当日止损 $8、杠杆 ≤2、名义 ≤ 第一档深度 10%、最长持有 24h）；
scheduler.py：模式 != off **或还有未平配对**时每分钟调 step()；autotrade._live_preflight 只加了默认不变的 check_wallet 参数。周末策略流程没动。
开关：`python -c "from desk import config, pairs_exec as p; p.put(config.load(), mode='shadow')"`（先 shadow 看几天，再改 'live'；默认 off；状态 data/state/pairs_live.json）。
清除"停开新仓"：`python -m desk.pairs_exec --clear-halt`（先核对交易所持仓；每 24h 会再推送提醒一次）。
评审修复 1-12（修复→测试）：对账/不明单不自动收尾、认领、限频告警→test_reconcile_*；单腿即平→test_single_leg_*；dex 并集+先管仓位+数据失败仍平→test_client_dexs_*；
估值宽盘口/mark/stale→test_wide_book_*；杠杆回包/非零仓位不开/一币一对→test_leverage_error_*、test_entry_refused_*、test_one_live_pair_per_coin_*；pair_id→test_pair_id_*；
异常路径成交价→test_exception_*；off 仍管理+切模式告警+调度器→test_off_mode_*、test_scheduler_keeps_*；止损锁→test_stop_out_writes_24h_lock、test_lock_blocks_*；
flock/超时→test_step_is_serialized_*、test_data_timeout_*；资金费→test_funding_*；杂项→test_balance_failure_*、test_min_notional_*、test_halt_realerts_*。
测试：tests/test_pairs_exec.py 54 条；`python -m pytest -q tests` 共 355 条全过。真下单点：pairs_exec.py:186（ioc）、:179（改杠杆），调用处 :442、:727。
未决风险：
1. known_issues.md 仓库里不存在，未读到。2. 真实 PairsClient 的新方法（mark_px/funding_rows/fill_for_cloid/l2_snapshot）只用 stub 测过，上 live 前先 shadow 再最小额实测。
3. 认领不明单的价格是估算（px_est），盈亏有偏差；同币多张不明单认领不了，需人工。4. 手动仓位与我们反向时 reduce-only 不下单只告警。
5. 数据抓取在后台线程，超时后线程仍会跑完（不堆叠）；资金费按峰值仓位占比分摊，是近似。
