# 期权策略 v2 研究底稿（卡 R1 文献 + 卡 R2 收益校准，2026-10-25，Sonnet 联网整理；标"未核实"的数字不得当事实引用）

# R1 系统化期权策略的公开证据（供选 3-5 个回测）  2026-10-25
方法与限度：沙箱只能用 WebFetch 读网页摘要，PDF 无法下载，所以多数数字来自论文摘要页/CXO/Alpha Architect 等二手转述或摘要工具对 PDF 的读取，未逐表核对原文；这类一律带"未核实"。收益单位均为"占期权权利金/期权价格的收益"，不是占保证金，除非另注。成本口径为"成交价偏离中间价 = 报价价差的 x%"。

## S1 Goyal & Saretto (2009 JFE)：IV 与历史波动/IV 预测变化 -> 跨截面跨式
- 规则：每月对个股做 ATM 跨式(30-60DTE，持有到期)；按 HV−IV 缺口(工作论文)或"预测的 IV 变化"(PDF 版：当前 logIV、IV 相对 12 月均值、IV 相对 12 月均 RV 三项回归)分十组，多最高组、空最低组，零成本。两个版本的排序变量描述互相冲突，以哪个为准：未核实。
- 收益：中间价下多空跨式月均 12.9%，月标准差 13.2%(1996-2005，OptionMetrics)；对冲后的 call/put 数字未取得，未核实。VIX 上升月 17.6% vs 下降月 8.5%。
- 成本后：持有到期(约 47 天)多空 16.1%(中间价) -> 5.5%(成交=75% 报价价差)；按价差三分位，成本后 2.9%(高价差)到 8.6%(中价差)。
- 尾部：空头腿是裸卖跨式，月波动 13%；论文称不被常见风险因子解释。
- 发表后：Dallas Fed WP2214(IPCA)认为多数期权异象在因子模型下 alpha 缩水一半到四分之三，但 RV−IV、ATM IV、IV 斜率、IV 期限四个信号在多重检验后仍显著；其结论是"未对市场有效性构成显著挑战"。2005 年后的净成本独立复刻：未核实。
- 我们能测吗：部分。8 个标的做不了十分位截面，只能做各标的时间序列版(IV 取 15:30-16:00 中间价，RV 用日收盘价，可用 2024 年之前的收盘价算历史 RV)；到期价值用日收盘算，可行；约 33 个月样本，统计力弱。空头侧必须用价差，翼值要在 ±10% 内。

## S2 Vasquez (2017 JFQA)：IV 期限结构斜率 -> 期权收益
- 规则：每月按"远月 ATM IV − 近月 ATM IV"斜率分十组，多最高组、空最低组(跨式/裸 call/裸 put/delta 对冲 call/put 五种)。
- 收益(SSRN 2011 版，经 CXO 转述，1996-2007，约 260 只股票/月，月度)：跨式多空毛 27.1%，对冲 call 4.0%、对冲 put 3.5%；成本=1.1 倍报价价差后跨式 12.3%、对冲 call 1.5%、对冲 put 0.8%。另一较早版本(Alpha Architect 转述)：跨式 19.6% -> 成本后 5.5%/月，实际成本达报价价差 1.25 倍则不显著。已发表版数字：未核实，版本间不一致。
- 注意：是按期权价格算的收益，非占资本；样本不含 2008-09；佣金未计；空头腿含裸卖。
- 发表后：IPCA 论文里 IV 斜率、IV 期限仍显著(见 S1)。
- 我们能测吗：部分。14-60DTE 可取约 30 天与约 55 天两个期限的 ATM IV 算斜率；但跨截面只有 8 只，改用各标的自身历史分位。对冲版净收益(0.8-1.5%/月)大概率被我们的成本吃掉，跨式版也需要先过成本关。

## S3 财报前后的跨式
多头侧(Gao, Xing, Zhang 2018 JFQA)：
- 规则：财报前 3 天买入 ATM(股价/行权价 0.95-1.05) delta 中性跨式，持有到公告日(窗口[-3,0])；期权 10-60DTE。
- 收益：[-3,0]等权平均 3.00%(约每日 1.00%)，1996-2010，JFQA 摘要给 3.34%；非财报期跨式平均每日 -0.19%、每周 -2.09%。小盘、波动大、期权成本高的股票更高。
- 成本后：只有短到期撑得住。到期 4-10 天：每日 1.64%(成本=50% 价差)、0.61%(100% 价差)；11-20 天：+0.15%(50%)、-0.23%(100%)；21 天以上显著为负。我们的数据最短 14DTE，所以预期在成本后不赚钱。只有约 18% 的公司在期权到期前 10 天内公布财报。
- 发表后：2010 年后的独立复刻未找到，未核实。
- 我们能测吗：部分。NVDA/AMD/MU/INTC 每年约 4 次财报，2.8 年共约 45 个事件(需外部财报日历)；可作"14DTE 以上多头跨式应亏"的负对照。
空头侧(IV 在财报前被高估)：
- 证据：de Silva, So, Smith (Review of Finance)：散户在预期波动大的财报前买入，这类公告日跨式比低预期日少赚约 11%，之后两周再少约 9%，作者读作过度定价、不是信息；散户财报期期权平均亏 5-9%，价差成本约占投入 9%(高波动财报)；做市商是获利方。
- 成本后的卖方净收益：未找到任何论文给出，未核实。Dubinsky 等(2019 RFS)只证明财报事件溢价存在，不判断高低估；BSIC 页面说"不能由此得出卖方有效"。
- 尾部：卖方在单次大跳时亏损大(NVDA 财报日涨跌 10% 以上并不罕见，数字未核实)；必须用 iron fly/condor 或日历价差封顶，翼值受 ±10% 限制。
- 我们能测吗：部分，n 很小，只能当探索性检验。

## S4 方差风险溢价(VRP)收割，定义风险版本
- Cboe PUT：每月卖 1 个月 ATM SPX put，现金全额担保，持有到结算。1986-2015：年化 10.13% vs 标普 9.85%，波动约 10.1%，夏普 0.67 vs 0.47(毛，未扣成本)；2006-2015：6.6% vs 7.1%，最大回撤 -33% vs -51%。Cboe 官方 factsheet(截至 2026-09-30，自 2007-01)：年化 7.2%、波动 10.7%、最大回撤 -32.7%(月末口径；标普全收益 11.0%/15.4%/-50.9%)；日历年：2020 +2.1%(标普 18.4%)、2022 -7.7%(-18.1%)、2024 +17.8%、2025 +9.2%。最长回撤+恢复 30 个月(截至 2023-08)。2024-08、2025-04 的月内回撤：未核实。
- Cboe BXM(备兑看涨)：1986-06 至 2026-09 年化 8.6%，波动 10.6%，最大回撤 -35.8%；2020 -2.8%、2022 -11.4%、2024 +20.1%、2025 +8.9%。PUT/BXM 的收益大部分来自持有股权风险，不是纯 VRP。
- CNDR(铁鹰)规则：每月卖 ~20Δ put 和 call，买 ~5Δ put 和 call 作保护，余额放 T-bill。Cboe 称 35 年最大月度峰谷回撤 19%(标普 51%)，标准差 7.23%(1986-2015)，自 1986 年起标普有 11 个月涨跌幅达 10% 以上，CNDR 一个都没有；年化收益未取得：未核实。
- 去掉股权暴露：AQR 的 delta 对冲、卖 5% OTM put(1996-2016)：夏普 0.68(标普 0.32)，年化 1.5%、波动 2.2%(口径未核实)，最大回撤 -10%，beta 0.04，已扣 AQR 估计的成本；亏损集中在股灾(如 2008)。对冲不消除凸性风险。
- 尾部：Broadie-Chernov-Johannes(1987-2005)：ATM put 买方平均月亏约 30%，即卖方平均大赚，但偶发暴跌时收益剧烈反向、收益随时间不稳；Bondarenko：周度 WPUT 最大回撤 -24.2%、PUT -32.7%(2006-2015)。
- 成本：Cboe 指数全是毛收益、无成本；SPY/QQQ 价差/铁鹰的严谨净成本研究没找到。仅找到澳洲单股 36 个月且不计成本的铁鹰论文(质量低，只作旁证：卖波动的结构表现好，买波动差)。
- 发表后：PUT 2007 年后的相对表现(年化 7.2% vs 标普 11.0%)显示"卖 put 跑不赢大盘"，只改善波动和回撤。
- 我们能测吗：是，最适配。SPY/QQQ/SOXX/SMH 做 14-60DTE put 价差或铁鹰，持有到期，日收盘结算；期间含 2024-08 和 2025-04 两次冲击，可直接看尾部。注意 5Δ 翼可能超出 ±10%，改用 10Δ 或固定百分比。

## S5 截面异象与 IV 动量/均值回归
- Cao & Han (JFE 2013)：每月买入低特质波动、卖出高特质波动股票的 delta 对冲 ATM call(约 1.5 个月到期，持有 1 月)，多空约 2%/月毛(1996-2009)；成本=50%-100% 价差后从约 1% 降到约 0；大盘股更弱；熊市波动大。能测吗：否(需大量个股)。
- Bali & Murray (JFQA 2013)：用 delta 和 vega 都中性的两期权+股票"偏度资产"，风险中性偏度与收益显著负相关(偏好正偏度)；摘要页无数字、无成本：未核实。能测吗：部分(需 ±10% 外的价外腿，数据可能不够)。
- An, Ang, Bali, Cakici (JF 2014)：call IV 上升的股票下月上涨(五分位多空毛 0.97%/月)，put IV 上升预示下跌；这是股票收益信号、不是期权收益，成本未计。能测吗：部分，只能做 8 标的时序。
- Heston 等 (JF 2023) 期权动量：按过去 6-36 月跨式收益排序，1996-2019，成本前夏普至少是股票动量 3 倍；按完整价差成本，基础版平均接近 0，需十分位、剔除宽价差并结合反转才剩下。能测吗：否(需截面与长历史)。
- Zhan 等 (RFS 2022)：用 9 个公司特征预测 delta 对冲卖 call 收益，夏普>2，称成本后为正：能测吗：否。
- IV 均值回归：作为 S1 的预测变量(IV 相对 12 月均值)出现，独立证据：未核实。
- 指数-成分股离散度：1996-2007 成本后约 1.2%/月，夏普 0.82；按可执行买卖价跨式版 19.9% -> 11.5%/月；危机相关性升高时亏损。能测吗：否/部分(只有 4 只成分股且无权重)。

## S6 趋势/突破后买期权为何不赚
- Amin, Coval, Seyhun (2004，OEX 1983-1995)：强势上涨后 call、下跌后 put 被高估(集中在 OTM 短期)；趋势押注用标的更好，反转押注用期权，趋势后卖 call 最赚。即买方追趋势时买的是最贵的期权。
- 一般结论：Coval-Shumway 报 ATM SPX 跨式持有到期每周约 -3.15%(经 BSIC 页面转引，原文未核实)；Xing-Zhang：非财报期单股跨式每周 -2.09%。买方须付 VRP + 价差，已定价的趋势延续不足以覆盖。
- 修正方向：只在 IV 相对 RV 偏低(S1)或期限斜率有利(S2)时买入，作为过滤器；用我们的趋势信号做条件检验即可。能测吗：是。

## 成本与总体判断
- Muravyev & Pearson (RFS 2020)：平均有效价差约比常规估计低 25%；会择时的交易者低于常规的 40%；调整后有效价差约为报价价差的一半。但这依赖下单择时，我们 15:30-16:00 的 NBBO 中间价回测应对 0/50/75/100% 价差四档做敏感性，以 75% 为基准。
- 文献共同点：毛收益大、净收益多被成本吃掉；卖方优势更稳但有尾部；用成本后能存活的只有高流动、低价差的标的，恰好是我们的 8 个标的。

## 推荐回测顺序
1) S4 SPY/QQQ(+SOXX/SMH)定义风险卖波动：put 价差/铁鹰 + 以 IV−RV 过滤。 2) S1 的 IV−RV 条件做多(修复趋势买入规则)与高 IV 卖价差。 3) S2 期限斜率作为过滤。 4) S3 空头财报价差探索 + 14DTE 以上多头跨式负对照。

## 来源
[S1] https://docs.lib.purdue.edu/ciberwp/55  https://www.cis.upenn.edu/~mkearns/finread/CrossOptions.pdf
[S2] https://alphaarchitect.com/2014/01/equity-term-structure-and-option-returns/  https://www.cxoadvisory.com/volatility-effects/exploiting-the-implied-volatility-term-structure/  https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/equity-volatility-term-structures-and-the-cross-section-of-option-returns/F0A40E99FD2458367DD9A56A89783D38
[S3] https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/abs/anticipating-uncertainty-straddles-around-earnings-announcements/7B34877AD5E06304BA3C55FBA3219FDD  https://www.ruf.rice.edu/~yxing/straddle_201305_03.pdf  https://www.timdesilva.me/files/papers/losing_optional.pdf  https://bsic.it/straddling-outside-and-into-earnings/  https://ideas.repec.org/a/oup/rfinst/v32y2019i2p646-687..html
[S4] https://cdn.cboe.com/resources/indices/factsheet/CboeGlobalIndices_PUT-Index.pdf  https://cdn.cboe.com/resources/indices/factsheet/CboeGlobalIndices_BXM-Index.pdf  https://www.cboe.com/insights/posts/benchmark-indices-series-volatility-management-with-cboes-bfly-and-cndr-indices/  https://ir.cboe.com/news/news-details/2016/Study-Analyzes-Performance-of-CBOE-SP-500-SPX-Options-Selling-Indexes-02-23-2016/default.aspx  https://www.cxoadvisory.com/equity-options/performance-of-cboe-putwrite-indexes  https://www.cboe.com/insights/posts/generating-income-and-managing-risk-cash-secured-put-writing-in-a-low-equity-return-environment  https://www.aqr.com/-/media/AQR/Documents/White-Papers/Understanding-the-Volatility-Risk-Premium.pdf  https://www.cxoadvisory.com/?p=1329  https://www.indexologyblog.com/2016/02/18/paper-by-professor-bondarenko-has-intriguing-new-analysis-of-put-and-wput-indexes/  https://ojs.aut.ac.nz/applied-finance-letters/article/download/69/61
[S5] https://www.cxoadvisory.com/volatility-effects/extracting-a-volatility-premium-with-equity-options  https://optionmetrics.com/research/t-g-bali-and-s-murrary-does-risk-neutral-skewness-predict-the-cross-section-of-equity-option-portfolio-returns  https://www.cxoadvisory.com/volatility-effects/stock-returns-and-changes-in-implied-volatility  https://alphaarchitect.com/option-momentum/  https://ideas.repec.org/a/oup/rfinst/v35y2022i3p1394-1442..html  https://quantpedia.com/strategies/dispersion-trading/  https://www.dallasfed.org/-/media/documents/research/papers/2022/wp2214.pdf
[S6/成本] https://www.cxoadvisory.com/equity-options/effect-of-stock-market-momentum-on-index-options-prices  https://ideas.repec.org/a/oup/rfinst/v33y2020i11p4973-5014..html  https://www.cxoadvisory.com/equity-options/timing-option-trades-to-suppress-trading-frictions/

---

# R2 期权收益现实校准（2026-10-25；[n]=文末来源；"自算"=我用Python算，非来源数据）
结论：周10%/月30-50%没有任何可核实的长期先例；公开期权/卖波动基金年化多在8-22%，且曾一次性回吐数年收益；"无限资金"不解决风险，也不提高收益率。
## 1 基金与策略的真实数字
- LJM Preservation&Growth：财年(至2017/10) A类 +8.56%，此前三年 +15.48%/+12.07%/+1.67%；净资产$7.68亿 [2]。2018/2 一周跌逾80%(Morningstar) [1]；SEC称亏超$10亿、约占总值80%，3月底清盘，其"最坏单日亏损"披露被指低估 [3]。
- Allianz Structured Alpha：2020/3 市值损失超$70亿，其中本金损失超$32亿；客户为养老金等机构；检方称对对冲/风险有误导 [4]。
- Malachite：约$6亿；2016 +22%、2017 +21%、成立至2020/1年化约10%(内部文件)；2020/3 因保证金追缴宣布解散，亏损幅度未披露 [5]。
- Optionsellers.com(2018/11 天然气暴涨)：券商清算约300账户，并向客户追索$3530万；总亏损额未核实 [6]。
- XIV：2018/2/5 单日-96.3%(NAV 115.55→4.22)，前日规模$19亿 [7]；最终赎回价$5.99 [8]。
- Catalyst Hedged Futures：2016 +6.2%，规模约$22亿；2017/2 一周-14.1%(同期标普+2.5%) [9]。2018年表现未核实。
- Universa(尾部对冲)：2020/3 +3612%，年初至3月末+4144%(其信件，经Bloomberg引述，付费墙未读全) [10]；长期净值/平时"流血"幅度未核实。它是保险，不是赚钱机器。
- Cboe PUT指数：1986/7-2008/10 年化10.32% vs 标普8.77%，波动9.91% vs 15.39% [11]。卖权溢价是真的，量级是个位数到低双位数。
- 标杆：Medallion 1988-2018 毛66.1%/净39.1%每年，认为容量上限约$100亿后退还外部资金 [12]。全球最强也只是年化~40%，不是周/月。
- Eurekahedge/HFR波动率对冲基金指数的具体年化与最大回撤：本次未找到可核实原文，未核实。
## 2 做市商钱从哪来
- Jane Street 2025净交易收入约$396亿(Hedgeweek转引Reuters匿名来源)，Citadel Securities约$122亿，HRT约$123亿；净交易收入≠利润，期权占比未披露 [13]。
- Optiver 2025：净交易收入EUR45.6亿，净利EUR17.7亿，年末权益EUR54.9亿 [14]；自算净利/平均权益约34%。官方页面"Key figures"与正文有一处数字冲突，需对年报。
- Citadel Securities 2025Q1净交易收入$34亿，同比约+45% [15]；利润率/期权份额未核实。SIG(Susquehanna)：未找到可靠财务数字，未核实。
- Jane Street印度：FY2025(至3月)净交易收益约$5.17亿，为前年约6倍；SEBI指控两年多获利超$40亿并涉嫌操纵Bank Nifty，已存$5.67亿托管后恢复交易，指控未终局 [16]。
- 零售期权流量占成交量逾60%，三家批发商处理近90%的付费订单流；零售平均买卖价差12.6%，零售平均亏钱 [17]。即零售是做市商的收入来源，不是对手。
- 做市利润来源(价差、规模、低延迟、库存对冲、ETF套利)属行业常识，本次未逐项找到一手来源，按"未核实"对待。
- Alpaca可达范围：Level1-3(备兑卖call/现金担保卖put、买call/put、垂直价差)；仅market/limit、当日有效；mleg要求每条腿都被覆盖(不能裸卖)；无盘后；零股期权不支持 [18]。无交易所做市资格、无返佣、无低延迟，PFOF/ETF套利/做市价差收入均不可达，只能当价差支付方做方向/波动率溢价/定义风险价差。
## 3 容量：资金越大%收益越低
- Medallion自己都因容量把外部资金退掉 [12]。卖波动率者合计约$1.5万亿(误差可达2倍)，拥挤且同向，VIX产品未平仓量远小于相关资产 [19]。
- Muravyev&Pearson：会择时执行的交易者有效价差不到传统度量的40%，整体平均低约1/4 [20]。成本可压，但前提是小单、会择时。
- 推理(非来源)：期权每个合约盘口深度有限，资金放大后吃多档、冲击成本上升，可用行权价/到期日有限，%收益只降不升。无限资金唯一作用是扛得住单次爆仓，但LJM/Malachite/Optionsellers都死于保证金与对手方，而非"本金不够多"。
## 4 数学(自算)
- 周10%复利：1.1^52=142倍/年(+14,100%)，两年约2万倍；等价月≈51%。月30%=23.3倍/年(≈6.2%/周)；月50%=129.7倍/年(≈9.8%/周)。对比Medallion净39%/年。
- Kelly：赢率55%、赔率1:1 时 f*=10%/笔，全Kelly每笔对数增长仅约0.5%；押30%每笔增长-1.6%，押50%为-8.9%。赢率60%：f*=20%，押30%增长+1.5%，押50%为-3.4%。超过约2倍Kelly增长率转负(理论结论)。真实期权edge远小于上述假设(未核实)。
- 模拟(104笔，每笔押权益f，1:1)：赢率55%：f=10%曾跌破起点10%的概率0.6%；f=30%为62%；f=50%为96%(中位终值约0.00008倍)。赢率60%：f=30%为28%，f=50%为81%。均值被极少数幸运路径拉高，中位数才是普通人的结果。
- 马丁格尔(输了加倍)：破产只需连输k次。公平游戏、本金=2^10-1个单位，1000轮破产约39%；2^20-1单位、10万轮约4.7%；且每轮要押的钱指数增长，会撞上券商保证金、持仓限额、单笔流动性与时间上限。期权卖方亏损可超本金(Optionsellers客户反欠券商$3530万) [6]。"无限资金"在现实中不存在。
## 5 零售可达、持续>1年的30%/月
- 未找到任何可信(第三方审计且>1年)案例。
- Benzinga页面"每20天27%、胜率83%"是促销文案，无交易记录/周期/样本，不采信，未核实 [23]。
- US Investing Championship 2024(实盘账户追踪，含期货期权的enhanced growth组)：<$100万组冠军+482.6%/年(自算约15.8%/月)，>$100万组冠军仅+90.7%；单年、自选参赛、有幸存者偏差，页面未说明核验方式，也非期权专属 [22]。
- 零售0DTE：Muenster研究估计散户合计每日亏约$35.8万 [21]；JoF 2023零售期权平均亏 [17]。
## 给团队的建议(判断，非事实)
- 把"10%/周"从目标改为上限幻想；合理目标按年化10-25%、最大回撤<20%、单笔风险<=1-2%权益设定，用于验证edge而非追收益。
- 用定义风险价差(Alpaca Level3可做)，不做裸卖/加倍；先以纸面+小资金核算含12.6%级别价差的真实成本。
## 来源
[1] https://investing.com/news/stock-market-news/us-fund-that-lost-most-of-its-value-shuts-doors-to-new-investment-1220052
[2] https://www.sec.gov/Archives/edgar/data/1552947/000158064218000199/ljmncsr.htm
[3] https://www.wealthmanagement.com/regulation-compliance/sec-charges-portfolio-managers-misleading-investors-over-funds-risks
[4] https://justice.gov/usao-sdny/pr/chief-investment-officer-allianz-global-investors-us-pleads-guilty-investment-adviser
[5] https://www.thewealthadvisor.com/article/600-million-volatility-fund-hits-margin-wall
[6] https://www.bloomberg.com/news/articles/2018-11-29/broker-seeks-millions-from-customers-of-wiped-out-fund-manager
[7] https://www.etf.com/sections/news/inverse-vix-etn-shuts-down
[8] https://www.slcg.com/pdf/workingpapers/Material%20Misrepresentations%20in%20XIV%20Prospectus%20Led%20to%20$700%20Million%20in%20Losses.pdf
[9] https://investmentnews.com/misclassified-fund-draws-huge-assets-then-falters-70665
[10] https://www.bloomberg.com/news/articles/2020-04-08/taleb-advised-universa-tail-risk-fund-returned-3-600-in-march
[11] https://en.wikipedia.org/wiki/CBOE_S%26P_500_PutWrite_Index
[12] https://www.wealthmanagement.com/alternative-investments/jim-simons-built-the-best-hedge-fund-ever
[13] https://www.hedgeweek.com/jane-street-outpaces-rivals-with-record-40bn-trading-haul/
[14] https://www.optiver.com/about-us/financial-results/
[15] https://www.bloomberg.com/news/articles/2025-05-27/citadel-securities-mints-3-4-billion-in-record-first-quarter
[16] https://www.businesstoday.in/amp/markets/stocks/story/jane-streets-india-arm-saw-six-fold-surge-in-trading-gains-before-sebi-clampdown-report-511483-2026-01-19
[17] https://lbsresearch.london.edu/id/eprint/2827
[18] https://docs.alpaca.markets/docs/options-trading-overview ; https://docs.alpaca.markets/us/docs/options-level-3-trading
[19] https://hedgefundalpha.com/strategies/short-volatility-strategies-are-extensive-and-widespread/
[20] https://ideas.repec.org/a/oup/rfinst/v33y2020i11p4973-5014..html
[21] https://www.bloomberg.com/news/articles/2023-04-21/day-traders-lose-358-000-per-day-gambling-on-zero-day-options
[22] https://www.businesswire.com/news/home/20250127956815/en/2024-United-States-Investing-Championship-Final-Results
[23] https://www.benzinga.com/amp/content/30376793
