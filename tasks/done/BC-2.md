# 任务卡 BC-2：熊市看涨价差（bear call credit spread）引擎定价验证（中档，Sonnet）
新增 tests/test_bear_call_spread.py，共 14 条，全部通过（0.7 秒/条量级，总 ~5 秒）；全合成数据，夹具沿用 test_options_lab 的 meta 收盘 + cbbo1m parquet 写法，走 validate_proposal -> to_strategy -> _simulate；期望值全部在测试里用报价表独立算。引擎代码一行没改。
覆盖：1 分类 vertical/credit + 默认出场档 + 只卖不买 = naked_short（含两卖一买、买 put 封 call、反向 = 借方、run_dev 不记试验）；2 常数报价精确到分（权利金 1.4144、max_loss 358.56、pnl −23.76、手续费 4×0.65）；
3 慢涨（止损恰好在第一个 成本−权利金 ≥ 2×权利金 的日子执行）/跳空冲破 1.08（第一天止损）/止损后再暴涨不多亏；4 下跌 -> 止盈（出场日成本 ≤ 50% 权利金，之前没有一天够到）、横盘 -> 衰减为正；
5 trend_R2 只用 d−1：d 当天收盘暴涨/乱改，决策不变；改 d−1 的收盘则翻转；入场日 = 首个信号日 +1；6 缺报价：止盈日缺长腿 -> 不止盈、顺延、盯市不比前一天好；跳空日缺短腿 -> stop_on_missing 封顶最大亏损；入场后永远没短腿报价 -> no_more_quotes 按最大亏损；exit_dte 日缺腿 -> 顺延一天。
变异检查：把 trend 改成含当天、把缺腿下界废掉、去掉滑点、把止损改 3 倍，测试都会变红。
全仓库：959 过、2 败、1 跳过。2 个失败都与本卡无关、改动前已存在：test_qa::test_functional_and_security_all_pass（已知，沙盒 QA 探测返回 200）；test_pairs_exec::test_pairs_client_parses_ioc_response_and_hashes_cloid（沙盒没装 hyperliquid SDK，完整 pip install -r 超时没装上）。
未发现 bug。一条待裁定（没改代码）：正常出场路径的亏损可以超过 max_loss。出场滑点是每条腿"权利金"的 1%，深度实值腿权利金很大。复现：入场后第 4 天标的 +30（124，两腿都深度实值），pnl −455.64，max_loss 401.45（−113%）；
位置 desk/options_v2_backtest.py:316 fill_price / :354 settle（正常路径不封顶），而同一笔如果走 stop_on_missing 路径会被 min(L,…) 封在 −max_loss−1.3，两条路径不一致。方向是保守的（多亏不是少亏），所以没动；测试按"max_loss + 手续费 + 出场滑点"当下限，并在报告里留痕。若要改：settle 正常路径对信用价差把平仓成本封在 L（+手续费）。
另两点说明（设计如此，不算问题）：max_loss 不含手续费；研究室的 trend 信号按 d−1 收盘，比 v1.signal（含 d 当天价）晚一天，入场用 d 当天 15:59 报价。
