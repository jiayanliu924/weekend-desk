# Card B：多空辩论闸门（完成）
- 新增 desk/debate.py：build_context（带编号事实 F1..Fn：标的现价、5/20 日涨跌、提案/最大亏损、新闻标题）→ 多头/空头(Haiku) → 风控官(Sonnet) → 代码算最终结果
- 无引用/引用不存在的观点丢弃并计数；size_mult 夹到 [0,1]；否决条件=风控 veto / 反对且把握≥0.6 / 仓位系数<0.25；只能否决或缩小
- 出错（LLM/超时/预算/解析/费用>0.30）→ on_error，默认 skip（不下单，记录）；复用 agents 的 AnthropicLLM/parse_json/spend/_record_spend
- 每次运行存 data/debates/<id>.json；CLI：python -m desk.debate --last N（中文摘要）
- config.toml 新增 [debate]（enabled=true 默认，false 则直通）
- alpaca_exec.open_for_sample：qty 算出后、payload 前调用；否决/出错 → skip（含辩论编号）；否则 qty=floor(qty*size_mult)，0 张 → skip；摘要写进账本 debate 字段和手机推送
- alpaca_exec.Client 新增 bars()（日线）；open_for_sample 新增 debate_llm 参数；debate.LLM_HOOK 供测试替换
- tests/test_debate.py（14 个）；tests/test_alpaca_exec.py 的 s fixture 关掉 debate（其余旧测试不变）；全量 453 passed
- 未碰 desk/supervisor.py、desk/scheduler.py，未提交
## 评审第 1 轮修复
- 真 LLM 客户端 25s 超时、不重试；整场辩论 [debate] deadline_s=75，每次调用放线程里按剩余时间等，超时走 on_error；辩论后重新取报价、重算限价/每张最大亏损，张数只减不增
- 风控官输出严格校验：veto 必须是 bool，size_mult/confidence 必须是 [0,1] 数字，direction 合法，否则算解析失败（skip），不再夹值
- 临时错误（超时/网络/预算）写 debate_error 而非 skip，同一 sid 下次锁定可重试，最多 2 次，第 2 次失败转永久 skip；预算用完推送 high 提醒
- 新闻查询带 a_ns/b_ns；风控官提示词也标注新闻/发言为外部数据
- 测试 484 passed（新增 deadline、畸形输出、重试、预算报警、二次报价）
