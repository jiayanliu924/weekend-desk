# 任务卡 C：pairs_exec 开仓守卫（HIGH，真钱）
改了什么（desk/pairs_exec.py，配置在 config.toml [pairs]）：
1. 休市禁开：纽约时间周五 19:30 → 周日 20:35 不开新仓（blackout_start/end/tz，设 "" 关闭；时区读不到则保守禁开）；平仓/止损照跑。
2. wrong_way 反向止损：开仓记 s_entry；同向价差 ≥ 2×|s_entry| 或 |价差| ≥ 300 bps → reduce-only 平仓 + 24h 锁。K 线价差、实时价差（盘口太宽时不用）任一触发。
3. 腿不活跃：最近 6 根 5m 里零成交（含缺 K 线）≥ 3 根，或一腿收盘价不动而另一腿动 >10 bps → 不开。
4. 下架/暂停：load_data 从 universe 原始 meta 取 isDelisted/halted/isHalted/tradingHalted → data["halted"]；不开，已有仓位以 "halted" 平掉（即使 discover_pairs 已把该对剔除）。字段缺失则跳过。
5. 透明度：live/shadow 每步写 data/state/pairs_last_decision.json；--status 打印中文决策表；每步一行 INFO 汇总。
测试：tests/test_pairs_exec.py 新增 22 条；全仓库 420 条全过。
夹具改动：T0 +3 天（避开默认休市窗口）；make_data 两腿加共同微小噪声（价差不变，免得被"腿不活跃"误伤）；一条旧测试涨幅 1%→0.4%（否则合法触发 wrong_way）。
未决：下架字段名未对真数据验证；发现数据缓存 6h；美股假日未单独处理；requirements 加了 tzdata。
