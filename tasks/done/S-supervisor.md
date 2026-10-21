# 任务卡 S：巡检员 supervisor（中档，只读）
新增 desk/supervisor.py（不碰 alpaca_exec.py / debate.py / pairs_exec.py）。checks(settings, now) 五项全是代码、各自 try/except，坏一项=一条 warn：
1. 真钱线空转：套利（48 开市小时无开仓，附决策表+账本前三原因）、周末自动下单（8 天无下单）、期权（能走真钱且有跳过记录）
2. 数据健康：journalctl 24h 超时次数（无则 info 跳过）、hl_ctx/news 多久没更新、套利决策表停更
3. 亏损预算：总止损 ≥50% warn / ≥75% alert、已触发 alert；套利单日止损同阈值
4. 大盘 SOXX/QQQ/SPY（Alpaca 日线，只读行情端点）：连跌/5日/10日/离20日高；alert 条件按你给的；正文带 gate_checks(check_clock=False) 的期权线状态
5. 磁盘/内存 <15%
job_hourly 去重：同 key 6h 内不重推，升级立刻推，同级别最多连推 4 次；job_daily 07:05 PT 一条日报，最多 1 次 Sonnet（失败也算一次、不重试），失败/无 key/超预算 → 代码日报。状态 data/state/supervisor.json。
调度：scheduler.due_jobs 加 supervisor:<小时> 与 supervisor_daily:<日期>，run_job 里放后台线程跑（不占每分钟的下单/止损循环）。config.toml 加 [supervisor]。CLI：--check / --daily --dry。网站概览加一小块「巡检员发现的问题」（读上次每小时结果）。
测试 tests/test_supervisor.py 15 条；全仓库 468 条全过。
未决/偏离：周末线、期权线是事件型，"48h 无入场"改成各自的合理窗口；"抓数据超时"只能靠 journalctl（pairs_exec 没落盘，没改它）；regime 另加了 warn 档（4 天/-5%/-7%）；日线含今天未收盘那根。
评审第 1 轮已修：①scheduler 里 supervisor.due 加 try/except，due() 对配置写错（"07:05"/None）用默认值；②journalctl 改 Popen 逐行流式数、20 秒杀掉、stderr 非空/无权限/rc≠0 → 当作查不了；③日报没推成则每个加州整点补跑直到当天推成；④重推次数按 key 累计不分级别（升级仍立刻推）；⑤_alpaca_bars 关闭 http 客户端；Ledger.all() 容忍最后一行写到一半（desk/ledger.py）。新增 5 条测试，全仓库 490 条全过。
