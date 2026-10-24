# 开源工具调研（卡 R3，2026-10-25，Sonnet 联网；星数/活跃度未核实）

# R3 开源工具/Skills 调研（2026-10-25）
核验方式：沙箱里 GitHub 网页/API、arXiv 被拦，只能读 raw.githubusercontent.com（README/LICENSE/源码）和 PyPI。所以【星数、最后 commit 日期一律 未核实】；活跃度用 README 新闻日期/PyPI 发布日期代替。原始抓取在 research/raw/。

## 1. Claude Agent Skills（SKILL.md）
- anthropics/skills — https://github.com/anthropics/skills ；Apache-2.0（README 称多数 Apache 2.0；docx/pdf/pptx/xlsx 是 source-available，非开源）。无任何金融/量化 skill；可用的是 skill-creator 和 template（把我们的研究规则写成 skill）。
- anthropics/financial-services（marketplace 名 claude-for-financial-services；financial-services-plugins 的 README 与其完全一致，疑为改名）— https://github.com/anthropics/financial-services ；Apache-2.0（LICENSE 已读）。内容：financial-analysis(comps/dcf/lbo/3-statement/audit-xls)、equity-research(earnings、model-update、thesis-tracker、catalyst-calendar、idea-generation 等9个)、private-equity、investment-banking、fund-admin、KYC；全是基本面/估值/文书工作流，没有期权回测、没有统计检验。
  - 唯一沾边：partner-built/lseg 的 option-vol-analysis（隐含 vs 已实现波动率模板、25d RR/BF 表）——依赖付费 LSEG MCP，不能直接用，只可抄输出格式。thesis-tracker 的"论点-证据-更新"结构可借给我们的 hypothesis log。设计上"所有产出 staged 等人工签字、subagent 深度=1"也值得参考。
- 社区合集：ComposioHQ/awesome-claude-skills、travisvn/awesome-claude-skills、VoltAgent/awesome-agent-skills（均已读 README）——grep 过，没有期权/量化类条目；只有 Binance 的加密现货技能（不相关）。
- 零散量化 skill（质量一般，当素材不当依赖）：
  - shakeebshaan/claude-code-quant-skills，MIT。backtest-review 的 SKILL.md 已读：7 项检查（前视、幸存者、过拟合、成本、regime、仓位、成交真实性）+ SHIP/FIX/SCRAP 判决。偏加密，但可改写成我们的"回测审计员"agent 的 system prompt。
  - wshobson/agents，MIT。plugins/quantitative-trading/skills/backtesting-frameworks（85行，偏浅：偏差表+walk-forward）、risk-metrics-calculation、agents/quant-analyst。价值低。

## 2. 多智能体交易/研究框架
- TauricResearch/TradingAgents — https://github.com/TauricResearch/TradingAgents ；Apache-2.0；README 到 2026-10 仍在发 v0.7.0，活跃；原生支持 Anthropic（ANTHROPIC_API_KEY/Bedrock）。分析师(基本面/情绪/新闻/技术)→多空研究员辩论→交易员→风控→PM。只做股票/加密，无期权。
- virattt/ai-hedge-fund — https://github.com/virattt/ai-hedge-fund ；MIT；新版 `aihf` 终端应用，支持 Anthropic；数据靠 Financial Datasets API（股票），无期权。可抄：纸面交易的哈希链账本（NAV 防篡改）、执行前审批、kill switch。
- AI4Finance-Foundation/FinRobot — https://github.com/AI4Finance-Foundation/FinRobot ；Apache-2.0（LICENSE）/PyPI 标 MIT，不一致，以仓库为准；V0=AutoGen(教学)、V2=PydanticAI 桌面应用；偏股权研报。对我们价值低。
- pipiku915/FinMem-LLM-StockTrading — https://github.com/pipiku915/FinMem-LLM-StockTrading ；MIT；README 更新停在 2024-06，视为不活跃。思路：分层记忆(短/中/长期)+时间衰减+重要性打分。仅当要做"记忆检索"时参考。
- RndmVariableQ/AlphaAgent — https://github.com/RndmVariableQ/AlphaAgent ；仓库内未见 LICENSE（许可 未核实）；A股 CSI1000 因子 DSL + LLM 挖因子，不适用。论文 https://arxiv.org/abs/2502.16789 的三项正则（原创性、假设-因子一致、复杂度惩罚；据摘要印象，正文 未核实）思路可借。
- microsoft/RD-Agent — https://github.com/microsoft/RD-Agent ；MIT；PyPI rdagent 1.0.0（2026-09-23）；用 LiteLLM 后端，可接 Claude；NeurIPS 2025 论文 https://arxiv.org/abs/2505.15155 。`rdagent fin_quant` 跑因子+模型联合进化，但绑定 Qlib/A股 CSI300 股票截面，需要 Docker。不能直接跑我们的期权，只能抄架构。
- microsoft/qlib（pyqlib 0.9.7，2025-08-15 后无新版）— MIT。股票截面 ML 平台，对期权引擎无直接用处。

## 3. 量化库
- py_vollib — https://github.com/vollib/py_vollib ；MIT；PyPI 1.0.12（2026-06-01）。Black/BS/BSM 欧式 IV+Greeks。注意：单只股票期权是美式，需另算。
- py_vollib_vectorized — https://github.com/marcdemers/py_vollib_vectorized ；MIT；PyPI 最后发布 2021-02-28（陈旧，新 Python/numba 兼容 未核实）。用途：对 OPRA 整条链批量算 IV/Greeks；先小样本对拍再用。
- QuantLib（Python：PyPI QuantLib 1.44，2026-10-13）— https://github.com/lballabio/QuantLib-SWIG ；BSD 系宽松。用途：美式期权(有限差分/二叉树)、分红、曲线，给 py_vollib 做对照基准。
- vectorbt — https://github.com/polakowo/vectorbt ；Apache-2.0 + Commons Clause（不得把它当作卖点出售/托管收费）；PyPI 1.2.0（2026-10-19）。OHLC 向量化回测，不懂期权链，对我们价值低；内部私用没问题。
- OpenBB — https://github.com/OpenBB-finance/OpenBB ；Apache-2.0；PyPI openbb 5.0.0（2026-09-29）。数据聚合层；我们有 Databento，仅可补宏观/利率/日历类数据（具体 provider 未核实）。
- arch — https://github.com/bashtage/arch ；NCSA（宽松）；PyPI 8.0.0（2025-10-21）。已核实 README：IID/Stationary/Circular/Moving Block Bootstrap，且有 SPA(Reality Check)、StepM、Model Confidence Set。这是防数据窥探最该直接用的库。
- 过拟合：pypbo(esvhd/pypbo) = AGPL-3.0、依赖 statsmodels 0.8.0、PyPI 无包，不要用，自己写 CSCV（约60行）。更好选择：
  - fynance（MIT，PyPI 2.13.1，2026-07-06）：源码 research/overfit.py 已读，实现 CSCV/PBO(Bailey et al. 2015)，同模块族 research/guards 含置换检验与 deflated Sharpe（后者仅据注释，未逐行核）。
  - skfolio（BSD-3，1.11.0，2026-10-24，很活跃）：有 combinatorial purged CV，可做 CPCV 切分。
  - sharpebench（0.28.0，2026-07 才首发，General Liquidity 公司；MIT OR Apache-2.0）、alphaengine（1.0.2，2026-07 首发，QuantOS；Apache-2.0）：简介都声称含 DSR/PBO/SPA 等，但很新、来源小，质量 未核实——只当对拍参照，不当唯一依赖。
- mlfinlab：不是开源。LICENSE 是 Hudson & Thames 订阅制专有协议，GitHub 仓库仅用于提 issue；PyPI 上也查不到 mlfinlab。开源仿品 mlfinpy（MIT，2024-10 后停更）可看，famlafl PyPI 标 Proprietary，别用。

## 4. RD-Agent 与 TradingAgents 的循环（读源码/README）
RD-Agent（rd_loop quant.py、bandit.py、feedback.py 已读）：
- 循环：propose(HypothesisGen 选 action=factor 或 model) → Hypothesis2Experiment → coder(Co-STEER，带错误/知识库重试) → runner(Qlib 回测) → summarizer 反馈(LLM 对比当前结果 vs SOTA 的 IC、含成本超额年化、最大回撤，输出 "Replace Best Result" 布尔)。Trace 记录每轮 (假设,实验,反馈)，支持 session 断点续跑、并行 loop。
- 调度：LinearThompsonTwoArm 两臂 bandit（factor/model），奖励是 IC、ICIR、RankIC、ARR、IR、MDD、Sharpe 的加权向量；也可选 llm/random。
- 过拟合：只见固定时间切分 train 2008-14 / valid 2015-16 / test 2017-2020（docs），含手续费/滑点；在已读的 6 个文件里搜 overfit/deflated/multiple-test 命中 0。我的推断：SOTA 反复在同一窗口上"择优替换"，没有试验次数校正，存在数据窥探风险。
TradingAgents：分析→辩论→交易→风控→PM；Memory log 记录决策，持有期结束后"结算"真实收益和相对基准 alpha，LLM 写一段反思，下次 PM 读取近期教训；run_backtest 在 ticker×日期网格上评分，按"决策后第一个收盘价"入场。v0.4–0.5 的更新日志大量是前视/时点修复（说明这类 LLM 回测极易泄漏）。README 自己承认结果不可复现、非确定性；没有多重检验控制。
应该抄的：
1) RD-Agent 的 Trace + 与现任 SOTA 对比的晋级规则 + 断点续跑 + 失败反馈进 coder 重试；用 bandit 在"新想法/调参/换退出规则"之间分配预算。
2) TradingAgents 的"延迟结算+反思写入记忆"，但教训只能来自训练窗口，且每条教训带日期、算进试验次数。
3) 时点纪律：agent 只看到决策时点前的数据；考虑对 agent 隐去真实 ticker/日期，避免 LLM 记忆历史。
4) 我们自己补的（两者都没有）：全局试验计数 N → DSR；候选集上做 PBO(CSCV)；arch 的 SPA/StepM 对"最优 vs 基线"做检验；block bootstrap 给 Sharpe 置信区间；最后一段完全不碰的 holdout 只开一次；成本用 OPRA 真实买卖价差。

## 许可小结
可商用/私用：MIT（RD-Agent、qlib、ai-hedge-fund、py_vollib、fynance、FinMem）、Apache-2.0（TradingAgents、financial-services、OpenBB、FinRobot 仓库）、BSD/NCSA（QuantLib、skfolio、arch）。受限：vectorbt（Commons Clause，内部用可）、pypbo（AGPL，别嵌入）、mlfinlab（专有）。AlphaAgent 许可 未核实。
