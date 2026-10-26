# BC-1 完成记录（低档）

- 新建文件：
  - knowledge/proposals/bear_call_v1/BC1.json
  - knowledge/proposals/bear_call_v1/BC2.json
  - scripts/run_bear_call.sh（已 chmod +x，bash -n 语法检查通过）
- 哈希：BC1 = 25808a513b1f364e；BC2 = 3649c483d099c8b3
- 校验：两个文件 ok=true，structure = vertical，kind = credit。
- 展开结果：标的 QQQ / SMH / SOXX / SPY；出场 standard（exit_dte 7，止盈 0.5，止损 2.0，最长持有 25 天，冷却 1 天），均为 v2 登记的缺省值。
- 入场：BC1 = trend_R2；BC2 = trend_R2 + iv_rv_ge 1.2。
- 脚本相对字面指令多写了一行 set -o pipefail，否则 tee 管道会吞掉 python 的失败退出码。
- 没有运行回测，脚本只写不执行。
- /opt/weekend-desk 路径和 venv 未核实是否存在，首次执行前需确认。
- 未 git commit。
