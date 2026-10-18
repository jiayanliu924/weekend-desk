# 任务卡 B：跨 dex 同底层配对真下单（HIGH）
改了什么：新增 desk/pairs_exec.py（信号直接 import pairs_lab：discover_pairs/align/spread_series/run_variants/walk_forward；只用已收盘 K 线）；
config.toml 加 [pairs]（保守默认：每腿 $100、并发 1、总敞口 $200、单对止损 $4、当日止损 $8、杠杆 ≤2、名义 ≤ 第一档深度 10%、最长持有 24h）；
scheduler.py 仅在模式 != off 时每分钟调一次 step()；autotrade._live_preflight 只加了一个默认不变的 check_wallet 参数。周末策略流程一行没动。
开关：`python -c "from desk import config, pairs_exec as p; p.put(config.load(), mode='shadow')"`（先 shadow 看几天推送，再改 'live'；默认 off；状态文件 data/state/pairs_live.json）。
安全：IOC 限价+滑点上限；先下较薄腿、第二腿按第一腿实际成交定量；缺腿/部分成交立即 reduce-only 回补；平仓只按我们账本里的成交量 reduce-only，从不 market_close；异常用持仓差额判定，判不了就停开新仓并告警。
cloid：账本里是 `pairs-<标的>-<毫秒>-<序号>`，发给交易所的是它 sha256 前 128 位（交易所要求 16 字节十六进制），两者对应关系写在账本。
测试：tests/test_pairs_exec.py 33 条；`python -m pytest -q tests` 全部 334 条通过。
未决风险：
1. knowledge/known_issues.md 在这个仓库里不存在，没读到；请核对其中是否有和 HIP-3 isolated 保证金/最小下单相关的条目。
2. 真实 PairsClient（l2_snapshot 取盘口、IOC 回包解析）只用 stub 测过，没碰真交易所；上 live 前请先 shadow，并用最小金额真测一次。
3. 手动仓位与我们反向时 reduce-only 无法安全执行：不下单只告警，需要人工平；模式切 off 不会自动平已有配对（先等平仓再关，或手动平）。
4. 订单结果不明/回补失败会把 halt_entries 置 true（停开新仓），核对后在 pairs_live.json 里改回 false。
5. 回测用 K 线收盘价、真实用盘口；两腿薄、isolated 保证金下单腿可能先被强平；step 同步跑在调度线程里，K 线抓取每 5 分钟一次，慢网络会占用 job_loop。
