# 品种扩展（#13）：`desk/universe.py` 怎么用、怎么想

现在跑的 7 个合约（XYZ100、SP500、BRENTOIL、NVDA、TSLA、INTC、SMSN）是 2026-10-07 手挑的。Hyperliquid 的 HIP-3 dex
（trade.xyz 等）还在不断上新，这个模块的任务是：**自动发现所有"底层真市场会关门"的永续，按参考市场分好时间表，
过一道流动性门槛，用和线上一模一样的周末反向逻辑粗略回测，排个名，再把前 N 名写进 config.toml。**

## 一、发现：Hyperliquid info 接口

- `{"type":"perpDexs"}` → HIP-3 dex 列表（第一项是 `null`，代表主 dex）。
- 对每个 dex：`{"type":"metaAndAssetCtxs","dex":"xyz"}` → `[meta, ctxs]`。`meta.universe[i]` 有 `name`（如 `xyz:NVDA`）、
  `szDecimals`、`maxLeverage`、`isDelisted`、`onlyIsolated`、`growthMode`（有的 dex 暴露）；`ctxs[i]` 有 `openInterest`
  （张数）、`dayNtlVlm`（24h 名义成交额）、`funding`、`oraclePx`、`markPx`、`midPx`、`impactPxs=[买, 卖]`。
- 主 dex（不带 `dex` 参数）**只数个数，全部排除**。原因：BTC/ETH/SOL 这些加密永续 7×24 交易，没有"真市场关门 →
  链上独自定价 → 真市场重开"这一段。我们整套假设（备忘 7.1：无实质新闻则偏离被抹掉）依赖的就是这一段，
  加密永续根本没有"重开价"可以回归，所以不在这个策略的宇宙里。
- 原始返回原样存到 `data/universe_raw.json`，分类、门槛、回测全在本地做；服务器抓一次拷回来，本机可以离线重算。

## 二、分类：参考市场时间表

每个市场要映射到一张"周末关门 → 重开"时间表（`SCHEDULES`），字段和 config.toml 的 `[[market.instruments]]` 一一对应：

| 键 | 参考市场 | 周末窗（当地时间） | 时区 |
|---|---|---|---|
| `us_equity` | 美股单票 24/5 | 周五 20:00 → 周日 20:00 | America/New_York |
| `us_index` | 美指 23/5（CME Globex 口径） | 周五 17:00 → 周日 18:00 | America/New_York |
| `metals_energy` | 金属/能源 23/5 | 周五 17:00 → 周日 18:00 | America/New_York |
| `fx` | 外汇 | 周五 17:00 → 周日 17:00 | America/New_York |
| `kr_equity` | 韩股（trade.xyz 口径，同 SMSN） | 周五 20:00 → 周一 09:01 | Asia/Seoul |
| `jp_equity` | 日股 JPX | 周五 15:30 → 周一 09:00 | Asia/Tokyo |
| `hk_equity` | 港股 HKEX | 周五 16:00 → 周一 09:30 | Asia/Hong_Kong |
| `eu_equity` | 欧股 Xetra | 周五 17:30 → 周一 09:00 | Europe/Berlin |

映射顺序（`classify()`）：

1. `knowledge/universe_overrides.toml` 的 `[exclude]`：直接排除（写原因）。
2. `[markets]`：手工指定时间表，优先于一切规则。
3. 命名规则表 `_SYMBOL_RULES`：去掉 dex 前缀后按名字查（美指 / 金属能源 / 韩股 / 日股 / 港股 / 欧股 / 美股单票各一张表）。
4. 交易所后缀 / 前缀：`.KS` `.T` `.HK` `.DE` `KR:` `JP:` 之类。
5. 货币对正则：两个 ISO 货币码拼起来（`EURUSD`、`USD-JPY`）→ `fx`。
6. `[dex_default]`：整个 dex 兜底（默认不开）。
7. 都没中 → `unknown`，报告里列在"待人工确认"，**不会自动加**。把确认好的写进 `[markets]` 再跑一次。

为什么不把所有大写字母的名字都当美股？因为台股（TSMC 现货）、印度、巴西这类我们没有时间表的市场一旦被当成美股加进去，
决策时刻就是错的，整周样本都废了。认不出就停下来问人，比猜对 90% 划算。

## 三、流动性门槛（config.toml `[universe]`）

| 参数 | 默认 | 含义 |
|---|---|---|
| `min_day_ntl_vlm_usd` | 200,000 | 24h 名义成交额下限 |
| `min_oi_usd` | 100,000 | 持仓量（张数 × 标记价）下限 |
| `max_spread_bps` | 20 | `impactPxs` 卖/买 − 1 的上限 |
| `weekends` | 12 | 回测最近多少个周末 |

缺数据按不过。`growthMode` 只在报告里标一列：开着的时候手续费约九折，但那是冷启动补贴，会消失，
**回测和出手门槛一律按全额手续费**（教训簿 2026-10-11 #4）。

## 四、回测与排名

每个候选用它的时间表生成 `[[market.instruments]]` 段，直接喂给 `clock.weekend_for_friday`，和线上同一个时钟：

- 锚价 = 真市场关门前最后一根 K 线收盘价；决策价 = 重开 −15 分钟；平仓价 = 重开 +5 分钟之后第一根 K 线。
- 反向：|dev| > 门槛（挂单 3 + 吃单 9 + 半点差 + edge_buffer）时按 −sign(dev) 开仓；净收益扣全额手续费；不含 carry 和滑点。
- 输出：周末数、反转率（全部周末）、出手笔数、命中率、平均净 bps、$1000 名义累计 USD、最差一笔及其周末。
- 排名按平均净 bps，再按累计 USD。没拿到 K 线的候选排最后、注明原因。

K 线走 `backtest_lib.get_candles`（网络 → `data/backfill/` 缓存），所以服务器跑过一次后本机也能重算。
已在 config.toml 里的 7 个合约也回测一遍放在"对照"表里，方便看新候选和老合约在同一段时间里的相对表现。

### 相关性提醒（最重要的一条）

同一个周末，美股单票、美指、美国商品会被同一个周一宏观事件（关税、就业数据、战争）同向推动，它们的周末样本**不是独立的**。
报告里每个候选都给出它最近 N 个周末的收益（关门价 → 平仓价）和 XYZ100 的相关系数 ρ，并把 k 个候选的样本按
`Σn / (1 + (k−1)·ρ̄)` 折算成"有效独立样本"（ρ̄ 用各自与 XYZ100 相关系数均值的平方近似两两相关）。

结论性的用法：备忘 7.4 的"30 个周末才评估"要按有效样本数算。加 10 个美股单票不等于样本 ×10；
加一个韩股 / 日股 / 欧股，因为重开时刻不同、结算的不是同一个周一，分散价值更高。

## 五、怎么跑

沙盒 / 本机连不上 api.hyperliquid.xyz（代理 403），`scan` 会自己检测并打印下面的说明；测试全部用写死的 fixture，不碰网络。

```bash
# 服务器（能访问 Hyperliquid）
cd ~/weekend-desk
python3 -m desk.universe scan --weekends 12        # → data/reports/universe.md、data/universe.json、data/universe_raw.json
python3 -m desk.universe add --top 10              # 把排名前 10 写进 config.toml（幂等；只允许周一到周四）
python -m desk refreeze                             # 改完 config 必须重新冻结本周规则

# 本机离线复算（把服务器的 data/universe_raw.json 和 data/backfill/ 拷回来）
python3 -m desk.universe scan --fixture data/universe_raw.json
```

`add` 的规则：

- 只加 `ranking` 里的（已过分类 + 门槛），`--top N` 取前 N。
- 幂等：name 或 coin 已在 config.toml 就跳过，并打印"已存在"。新段插在 `[clock]` 之前，写回前先验 TOML 合法。
- 周五到周日拒绝改 config.toml（周五收盘到周日决策之间改动会让当周结果作废）；`--force` 可强行，不建议。
- 加完要做两件事：`python -m desk refreeze`；给 `desk/relevance.py` 的新闻相关性表补上新名字，否则新合约的"信息周末"判定是空的。

## 六、不做什么

- 不处理假日和提前收盘（那是 `windows_backtest` 的手写日历）；时间表只定义普通周末。
- 不自动改 `[risk]`：合约多了，`joint_first_loss_frac` 的联合压测会更紧，是否放宽由周一复核决定。
- 不给加密永续找借口：它们没有重开价，不在这个策略里。
