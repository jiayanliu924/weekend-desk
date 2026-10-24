# 期权事件波动率回测（DVOL 近似，2021–2026）＋ 美股口径（VIX 近似）

对应代码：`desk/options_backtest.py`（11b / 11c）。和 `desk/options.py` 线上研究、`desk/options_paper.py` 纸面交易员、`desk/options_shock.py` 突发样本是同一个问题的几个层次：
线上研究只从 2026-10 起有真实期权链，样本攒得慢；这个回测用 Deribit 的 DVOL 指数把样本拉长到 5 年，先看个大概。
真钱的落地场所是 Robinhood 的美股期权（美国居民用不了 Deribit），所以 11c 又加了 `--market us`：用 VIX/VXN 和日线做同一套计算，口径和真实交易场所对齐。

## 三类事件（线上研究 `desk/options.py` 的样本来源）

| 类别 | 哪里来 | 什么时候锁定 | event_type | 真钱对应的美股期权 |
|---|---|---|---|---|
| ① 排期宏观 | `events.toml [events]`：CPI / FOMC / 非农 | 事件前 60 分钟 | `CPI` / `FOMC` / `NFP` | SPY / QQQ（指数）、IBIT（BTC 敞口）|
| ② 财报 | `events.toml [earnings]`：NVDA / TSLA / INTC / SMSN…（`confirmed = false` 表示公司还没公告、按惯例估计）| 事件前 60 分钟 | `EARNINGS:<ticker>` | 单票期权（NVDA、TSLA、INTC；三星没有美股期权，只能看 ADR/ETF）|
| ③ 突发冲击 | `desk/options_shock.py`：抽取器吐出的重要度 ≥2 事件，归到 地缘 / 监管-ETF-SEC / 交易所被盗 / 宏观意外 / 关税 五类 | **新闻到达时立刻**（没有"事件前"）| `SHOCK:<类别>` | IBIT / SPY / QQQ |

注意：线上测的隐含和实际都是 **BTC（Deribit 链）** 的，财报样本看的是"英伟达财报前后 BTC 期权的隐含 vs 实际"，不是英伟达自己的期权——bot 没有单票期权链数据。
它回答的是"这类时刻 BTC 的波动溢价怎么样"；真要在单票上卖财报波动，得另看单票的隐含（Robinhood 期权链里直接看 IV）。
纸面交易员按 `EARNINGS:<ticker>` 分组攒历史，`SHOCK:<类别>` 同理；财报不改变每日对照组的分组口径（对照组只看窗口里有没有排期宏观事件）。

### 为什么突发事件只能"事后"做

排期事件可以在事件前 60 分钟锁定：那一刻的隐含是"市场对即将发生的事的定价"，事后比较才有意义。
突发事件没有这个时刻——新闻到达时它已经发生，期权市场已经（部分）重新定价。所以 ③ 问的是另一个问题：

> 冲击刚被定价之后的那一刻，隐含波动和随后 24 小时真正走出来的波动谁大？——也就是"突发之后卖恐慌 / 买延续"有没有边际。

这和 ① ② 的"事前预测"不是一回事，成绩单分开看（网页"突发事件样本"卡片，按类别和状态分）。

### 已结束 vs 进行中（纸面规则，写死在 `options_paper.shock_gate`）

抽取器给每条事件标 `event_status`：**concluded**（一次性、已经结束：一次袭击已发生、一份决议已通过）、**ongoing**（还在进行、可能升级或反复：战争持续、谈判未定、封锁未解）、**unknown**（原文看不出）。
教训簿 2026-10-11 原油那次的结论：已结束的事件 11 个里 8 个两天内反转；进行中的 6 个里 5 个延续，而且尾部双向。放到期权上：

- **concluded** → 和排期事件一样：同类别历史 ≥ `min_history` 个且均值 实际/隐含 < 0.9 → A 卖跨式；> 1.1 → B；否则 C。
- **ongoing** → **一律 C**。卖波动会被反复扫（每一轮升级都是新的跳空），买波动则在"吓唬完了没下文"时亏 theta；都没有可靠边际，先攒样本。
- **unknown** → 按最坏情况处理，**一律 C**。

三种状态的 实际/隐含 和 A/B 纸面盈亏都照常记，成绩单按状态分列；等 ongoing 组攒够样本、数据说"卖恐慌其实赚"，再改规则（改规则 = 改代码 + 周一复核）。

其余细节：同一件事 24 小时内再出现（event_id 相同 / 原句相似 / 同类别）记为重复不开新样本；新闻超过 6 小时才看到的不开（锁定时刻的隐含已经不是"刚重新定价"）；锁定失败（没期权快照）也记录，不算正式样本。

## 它测什么

**问题**：FOMC / CPI / 非农 / 周末之前，期权市场预期的波动（隐含）是否系统性高于随后 24 小时真正走出来的波动（实际）？
如果隐含长期偏高，事件前卖跨式（收权利金）有边际；如果偏低，买跨式有边际。

**每个事件一行**：

| 量 | 怎么算 |
|---|---|
| 隐含 IV | 事件前 1 小时（和线上 `event_lead_min = 60` 一致）的 DVOL ÷ 100 |
| 实际 RV | 从锁定时刻起 24 小时的永续小时收盘价，平方对数收益求和后年化 |
| 实际/隐含 | RV ÷ IV；几何均值用 ln 比值的均值取指数 |
| 跨式盈亏 | 按 IV 用 Black-Scholes 给 24 小时平值跨式定价（权利金），窗口结束按内在价值结算；名义 `[options.paper] notional_usd`（默认 1000 美元标的等值）|
| 费用 | 和纸面交易员同一套：每腿 0.03% 标的、封顶该腿权利金 12.5%，再加权利金 0.5% 滑点；进出各一次 |
| VRP | (IV² − RV²) × T，方差风险溢价，单位是方差 |
| 入场过滤 | IV ÷ 过去 7 天实际波动。只用事前能知道的量 |

**汇总**：按事件类型 × 年份给出样本数、几何均值 实际/隐含、卖方胜率、净盈亏、平均每笔、最差一笔、**去掉最好 3 笔后的净盈亏**（看盈利是不是全靠几次运气）。
买跨式另有一张表。门槛扫描：IV/过去 7 天 RV 分别 > 1.0 / 1.1 / 1.2 / 1.3 / 1.5 才卖，< 1/门槛才买。

**为什么分年**：加密对 CPI 的敏感度 2022 → 2025 变化很大（2022 年 CPI 是主线，2024–2025 年 ETF 资金和政策更重要），合在一起看会互相抵消。

## 事件日历

写死在代码里（`CPI_DATES / NFP_DATES / FOMC_DATES`），2026-10-12 逐年核对过：

- CPI、非农：BLS 各年发布日程（bls.gov/schedule/<年>/home.htm）。08:30 美东。
- FOMC：美联储日历（federalreserve.gov/monetarypolicy/fomccalendars.htm），取声明发布日 14:00 美东。
- 2025 年 10–12 月政府停摆改期：10 月 CPI 和 10 月非农**取消**；9 月非农改到 11/20，11 月非农改到 12/16；9 月 CPI 改到 10/24，11 月 CPI 改到 12/18。
- 周末：每个周六 00:00 UTC 当"事件"，锁定在周五 23:00 UTC，窗口 = 整个周六。`--no-weekends` 可关掉。
  **⚠ 周末行是口径假象，见下面"周末样本为什么不能信"。**

夏令时自动处理（美东 → UTC）。

## 假设与局限（读结果前必看）

1. **DVOL 是 30 天恒定期限的隐含波动**，不是 24 小时期权的隐含波动。事件前最短期期权的隐含通常比 DVOL 高出不少（事件溢价集中在近月）。
   所以这里的"隐含"系统性**偏低**：卖方真实能收的权利金更多，表里卖方的盈亏偏保守；买方的盈亏偏乐观。
   线上 `options.py` 用的是真实链按总方差插值到 24 小时的隐含，口径更准；这个回测只负责"方向对不对、量级多大"。
2. **没有波动率微笑**：按单一 IV 用 B-S 定平值跨式。平值附近微笑影响小，可以接受。
3. **实际波动用小时收盘价**，比分钟级偏低（漏掉盘中来回）。线上用分钟级指数价。
4. **结算按内在价值**，等于拿到期交割；真实 24 小时后平仓时期权还剩时间价值（取决于选的到期日），线上纸面交易员会用窗口结束时的链按标记价平。
5. **没算 delta 对冲**（纸面交易员有一次再平衡的近似）。
6. 费用按 Deribit 吃单费封顶规则 + 固定滑点，没有盘口深度；事件时刻真实点差会更宽。
7. 2021 年 3 月之前没有 DVOL 数据（指数 2021 年 3 月上线），`--since 2021-01-01` 实际从有数据的地方开始。
8. 永续价格 ≠ 期权结算用的指数价，事件时刻资金费率和基差会有几十 bps 的差别。

## 周末样本为什么不能信（2026-10-15 在真实数据上发现）

真实数据跑出来：465 个样本卖跨式累计 +$3,946，但几乎全部来自**周末**（每年卖方都赚 +$330–960），CPI / FOMC / 非农六年合计接近 0。

这不是"周末卖波动有钱赚"，是**量错了**：

- DVOL 是 30 天恒定期限的指数，里面混着工作日的波动；
- 周末到期的真实期权，市场早就知道"周末安静"，隐含本来就低；
- 拿 DVOL 当周末 24 小时的隐含，就等于假设能按工作日的"贵价"卖出周末期权 → **DVOL 近似在周末系统性高估卖方（周末真实期权已按安静定价）**。

反证：2026-10-08 起每天用真实 24 小时期权链锁定的对照样本，实际/隐含平均约 0.91——卖方几乎没有边际，远不像 DVOL 口径的周末那么好看。

所以现在：

1. 报告里 WEEKEND 行照常列出，但开头和结论里都明确标注上面那句话；结论（"假设是否成立"）只按排期事件下。
2. 回测门 `data/reports/options_gate.json`（`write_gate`）**只算 CPI / FOMC / NFP**，周末一律去掉；文件里写 `"method": "dvol_proxy"`、`"events_only": true`、`"n_weekend_excluded"`。
   执行层只认这种新版文件，旧版（可能含周末）当成不通过。
3. 执行层另加一道**真实报价门**（`options.real_chain_stats`，见 `alpaca_exec_README.md`）：只用本机存的 Deribit 真实期权链结算过的样本，
   按 每日 / 事件 / 突发 分组给出 样本数、几何均值 实际/隐含、纸面 A/B 净盈亏（全额手续费）、胜率、最差一笔。
   每次结算后写 `data/reports/options_real_gate.json`。通过条件：事件 + 突发 ≥ 10 个，且 A 或 B 累计 > 0，且最差一笔 ≥ −3 × 单笔风险上限。
4. 每次跑回测还会写 `data/reports/options_backtest_<币种>_summary.json`（按类型的 实际/隐含 和卖方净盈亏），网页"期权研究"页的
   "真实报价 vs 近似回测"小框拿它和真实期权链的数字并排放。

一句话：**DVOL 回测只能当方向参考；真钱前必须有真实报价的证据。**

## 真实成交价回测（`desk/options_real_backtest.py`，2026-10-18）

DVOL 口径的问题（上一节）的正解：用 Deribit 历史接口（history.deribit.com，`get_last_trades_by_currency_and_time` /
`get_last_trades_by_instrument_and_time`，`include_old=true`）拿已到期周期权的**真实成交**。

- 周五 08:00 UTC 到期；入场 周五 08:00（7DTE）/ 周一 08:00（4DTE），报价 = 入场前 60 分钟成交 VWAP（不含入场后），没成交就跳过。
- 有限风险结构：卖铁鹰（15Δ/5Δ、10Δ/5Δ）、卖铁蝶（平值/10Δ 翼）、买跨式；卖宽跨式 15Δ 只作参考。持有到期 + 50% 止盈两个变体。
- 费用每腿 0.03% 标的封顶 12.5% 权利金 + 滑点 2/5/10%（默认 5%）+ 交割费。
- 报告：按结构 × 年份、事件周 vs 安静周、滑点敏感性；含「历史最大回撤 ≤ 20% 的每周风险占比」和 bootstrap 52 周跌穿 −50% 的概率。
- 输出 `data/reports/options_real_backtest_BTC.md` + `_summary.json`（`method = deribit_real_trades`）；缓存 `data/options_cache/real/`。
- **没有合成模式**。沙箱连不上（代理 403）→ 退出码 2 并提示在服务器上跑；之后 `--offline` 只用缓存重算。

```bash
python3 -m desk.options_real_backtest --currency BTC --since 2023-01-01
```

## 怎么跑

**沙箱 / 本机连不上 deribit.com**（代理 403）：程序会检测到，打印"请到 bot 服务器上跑"并退出码 2。不会偷偷用合成数据。

**在 bot 服务器上**：

```bash
cd /path/to/weekend-desk
python3 -m desk.options_backtest --currency BTC --since 2021-01-01
python3 -m desk.options_backtest --currency ETH --since 2021-01-01
```

- 数据缓存到 `data/options_cache/{BTC,ETH}_dvol_1h.parquet` 与 `{BTC,ETH}_perp_1h.parquet`；再跑只增量补最新的，网络断了也能用缓存重算。
- 报告写到 `data/reports/options_backtest_<币种>.md`（`--out` 可改），同时打印到终端。
- 可选：`--until 2025-12-31`、`--no-weekends`。
- `--synthetic` 只用于测试程序逻辑：报告顶上会写明"合成数据"，文件名带 `_SYNTHETIC`。**不要把它当结果。**

**测试**：`python3 -m pytest tests/test_options_backtest.py tests/test_options_shock.py -q`（全部在合成数据上跑，不碰网络）。

## 美股口径：`--market us`（11c，VIX 近似）

**为什么要有它**：真钱在 Robinhood 做美股期权，不是 Deribit。同样的问题换成美股的数据再算一遍，结论才能对得上要下单的场所。

```bash
python3 -m desk.options_backtest --market us --underlying SPY  --since 2021-01-01    # 隐含 = VIX
python3 -m desk.options_backtest --market us --underlying QQQ  --since 2021-01-01    # 隐含 = VXN
python3 -m desk.options_backtest --market us --underlying IBIT --since 2024-01-01    # 隐含 = BTC DVOL 代理
python3 -m desk.options_backtest --market us --underlying SPY --fee-per-contract 0.03   # 改费用
```

| 量 | 怎么算 |
|---|---|
| 隐含 IV | 事件前一个交易日收盘的 VIX（SPY）/ VXN（QQQ）÷ 100。都是 30 天恒定期限指数，和 DVOL 一样是近似 |
| 实际 RV | 事件当天收盘 对 前一收盘 的单日对数收益，取绝对值 × √(π/2) × √252（单个样本的无偏标准差估计；噪声大，只看汇总）|
| 窗口 | 前一收盘 → 事件日收盘。CPI/非农 08:30 ET 和 FOMC 14:00 ET 都在窗口里。没有周末样本 |
| 跨式 | 1 个交易日平值跨式按 IV 用 B-S 定价，收盘按内在价值结算。张数 = 名义 ÷ (价格 × 100)，纸面允许小数张 |
| 费用 | `[options.us]`：每张合约每次成交 `fee_per_contract_usd`（默认 0.65 美元，常见券商口径；Robinhood 本身不收佣金，只有 OCC/交易所/监管费每张几美分，可用 `--fee-per-contract` 改）× 2 腿 × 进出；再加权利金/赔付的 `slippage_pct`（默认 1%）滑点。两腿都归零时不用平仓、省一次费 |
| 数据 | stooq.com 日线 CSV（`spy.us` / `qqq.us` / `ibit.us` / `^vix` / `^vxn`）；不通时退到 FRED（`SP500` / `NASDAQ100` / `VIXCLS` / `VXNCLS`）；都不通用 `data/options_cache/us_*.parquet` 缓存。沙箱里两家都是 403，要在 bot 服务器上跑（程序会提示、退出码 2）|

**IBIT 说明**：IBIT（贝莱德现货比特币 ETF）2024-01-11 上市，**IBIT 期权 2024-11-19 才上市**。IBIT 没有公开的隐含波动指数，这里全程用 **BTC DVOL** 当代理（Deribit 小时 DVOL 取每天 21:00 UTC 那一格）。
报告会标明上市前有多少行只是"假如当时有 IBIT 期权"——那些不是可交易过的历史。IBIT 期权真实的隐含要去 Robinhood 链上看，通常比 DVOL 略高（美股期权做市更保守）。

输出：`data/reports/options_backtest_US_<标的>.md`，表格格式和加密口径一样（按类型 × 年份、买卖两边、门槛扫描、逐事件）。

**读法上的差别**：美股的 CPI/FOMC 日 VIX 通常不会像 DVOL 那样明显抬高（事件溢价集中在当天到期的 0DTE 期权里，VIX 看不到），所以美股口径的"隐含"比加密口径更偏低，卖方盈亏更保守。要看的是方向和按年份的稳定性，不是绝对金额。

## Robinhood 执行备忘（机制说明，不是建议）

- **权限等级**：Robinhood 期权分 Level 2（买 call/put、备兑、现金担保卖 put）和 Level 3（价差、裸卖、跨式/宽跨式）。**卖跨式（A）是裸卖两条腿，需要 Level 3**，且保证金按裸卖规则占用（SPY 一张裸跨式通常要几千美元保证金）。买跨式（B）Level 2 就行，最大亏损 = 权利金。
- **有限风险的替代结构**（如果拿不到 Level 3，或不想承担无上限风险）：
  - **铁鹰（iron condor）**：卖一个虚值 call + 卖一个虚值 put，同时再往外各买一个更虚值的 call/put 保护。收到的净权利金比跨式少，但最大亏损被锁定在"翼宽 − 净权利金"。本质还是"赌实际 < 隐含"。
  - **铁蝶（iron butterfly）**：卖平值跨式 + 买两翼保护。权利金接近跨式、最大亏损有限，是和回测里"卖平值跨式"最接近的有限风险版本。
  - **信用价差（credit spread）**：只做一边（看涨或看跌），Level 3 的价差权限即可。
  - 这些结构的回测口径和跨式不同（翼的成本、更多腿的费用和点差），**本回测的数字不能直接套到它们上**；要用就另算。
- **到期日选择**：SPY/QQQ 有每日到期（0DTE）和周三/周五；单票（NVDA/TSLA/INTC）一般只有周五到期；IBIT 有周到期。回测里的"1 个交易日"窗口对应的是事件日当天或次日到期的合约。
- **结算**：美股期权是实物交割（到期实值会变成股票多/空仓），和 Deribit 的现金结算不同；不想接股票就在到期前平仓，多付一次费用和点差。
- **开盘时间**：美股期权只在 09:30–16:00 ET 交易（SPY/QQQ 部分合约到 16:15）。CPI/非农 08:30 ET 发布时期权市场还没开——**事件前 60 分钟锁定的隐含，在美股上要用前一天收盘的链**，这也是 `--market us` 用前一收盘的原因；FOMC 14:00 ET 则可以在盘中锁定。
- 以上是机制说明。真钱前照备忘第九章：先纸面、再极小仓，所有规则写进代码由程序执行（教训簿 2026-10-11）。

## 怎么读结论

- 看 `ALL / ALL` 那行的几何均值：< 0.9 且"去掉最好 3 笔"后仍为正 → 卖波动初步成立；> 1.1 → 买波动初步成立；其余 → 没边际。
- 再看最差一笔和权利金的比例：卖跨式尾部一次亏损能吃掉多少次收益。
- 门槛扫描里，门槛提高后胜率上升但笔数骤减，说明过滤有效；如果胜率不变，过滤没用。
- 这个回测的结论只用来决定纸面交易员（11a）的选边规则值不值得保留，**不是**实盘依据。备忘第九章：只研究、不做市、不卖期权、不下单。

## 期权策略 v2 回测（卡 B2，`desk/options_v2_backtest.py`）

预先登记的 v2 策略（`knowledge/options_strategies_v2.toml`，sha256 前 16 位 `9531c41e7262e5a1`，改一个数字 = v3）的真实报价回测：put 价差 F1/F1f、铁鹰 F2、便宜时买跨式 F3、财报铁蝶 F4 / 财报买跨式对照 F4c，共 28 个（策略, 标的）组合，累计尝试 60 次（Bonferroni 单次 p < 0.05/60）。
规格全部读 toml，代码里不写数字；v1（`options_rules_backtest.py`）的 Quotes / 选约 / 簇 / 自助法 / 补数据机制直接复用。口径与解释写在模块文件头，报告里也有“先读这几条”。

```bash
python -m desk.options_v2_backtest --fetch-closes            # 一次：8 个标的日线收盘（Alpaca 只读，RV20 要 2024 年之前的收盘）
python -m desk.options_v2_backtest                           # 第一遍：写 data/opra/meta/extra_needed_v2.json，打印下一步命令
python -m desk.opra_fetch --extra --needed-file extra_needed_v2.json --extra-sub cbbo1m_extra_v2 --plan
python -m desk.opra_fetch --extra --needed-file extra_needed_v2.json --extra-sub cbbo1m_extra_v2 --download --yes
python -m desk.options_v2_backtest                           # 第二遍：合并主数据 + 补数据，出报告
python -m desk.options_v2_backtest --strategies F1 F2 --underlyings SPY QQQ    # 只跑一部分
```

- 输出：`data/reports/options_v2.md`（顶部 5 行大白话：谁过关、三档月收益、最大回撤）、`options_v2.json`、`options_v2_trades.csv`。
- 补数据放在 `cbbo1m_extra_v2/`（v1 的 `cbbo1m_extra/` 照读但不写；同一目录下已有的月文件会被 `opra_fetch` 跳过，所以 v2 必须用自己的目录）。补完仍缺的是 Databento 也没有的报价，回测按保守口径处理（空头腿无卖价 = 该价差按最大亏损平仓；多头腿无买价 = 0；一直没完整报价 = 最大亏损），报告里单独计数。
- 财报单需要 `knowledge/earnings_history.json`（`[{"ticker","date","timing":"amc|bmo","source"}]`，卡 E1 负责）；文件不存在时 F4 / F4c 只报“没有财报日历”，不报错。
- 参考口径（中间价 ± 75% / 50% 半价差）**仅参考，不作为结论**；“好得离谱”（考试段每笔平均 > 最大亏损的 40%，或胜率 > 95% 且 ≥30 笔）先当 bug 查，不算过关。
- 仓位换算：三档“每笔最大亏损占账户比例”（2% / 5% / 10%），张数允许小数，同时持仓 ≤ 6，同一天开的 SPY/QQQ/SOXX/SMH 视为完全相关（合计 ≤ 3 笔）；报告给考试段月均 / 中位、最差一周、最大回撤和四分之一凯利。
- 沙箱没有数据：`python3 -m pytest -q tests/test_options_v2_backtest.py` 全部用合成报价；真数据只能在服务器上跑。
