"""命令行入口。

python -m desk run              启动（采集 + 周末自动流程），服务器上由 systemd 常驻
python -m desk status           查看数据、新闻源、本周进度
python -m desk backfill         回补历史 K 线 + 基线描述统计（7.7 第 3–4 周）+ 期权历史粗看
python -m desk report           立刻生成最近一个周末的周报
python -m desk note "..."       写下本周"假设与改动"（周五收盘前）
python -m desk review 2026-10-09 "..."   周一人工复核，写入复盘
python -m desk check-llm        抽检：随机取最近新闻跑一次抽取，打印结果
python -m desk adduser 名字      创建网页登录账号（在服务器上输入密码）
python -m desk deluser 名字      删除账号
"""
from __future__ import annotations

import asyncio
import os
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import backfill, config, evaluate, jobs, rawstore, report
from .clock import current_or_next_weekend, last_completed_weekend
from .ledger import Ledger


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    s = config.load()
    cmd = argv[0] if argv else "status"
    now = datetime.now(timezone.utc)

    if cmd == "run":
        from .scheduler import main as run_main
        asyncio.run(run_main(s))
    elif cmd == "status":
        w = current_or_next_weekend(now, s)
        print(f"品种 {s.instrument} | 模式 {'实盘' if s['live']['enabled'] else '模拟'} | 规则哈希 {config.rules_hash(s.root)}")
        print(f"本周末 {w.wid}: 周五收盘 {w.close:%m-%d %H:%M} UTC，决策 {w.decision:%m-%d %H:%M} UTC，恢复 {w.resume:%m-%d %H:%M} UTC")
        for st in ("hl_ctx", "hl_book", "hl_trades", "hl_params", "news", "events", "drb_index", "drb_opts", "drb_dvol"):
            rows = rawstore.query(s.data, st, select="count(*), max(received_at)", order="1")
            n, last = rows[0] if rows else (0, None)
            ago = f"{(time.time_ns() - last) / 60e9:.1f} 分钟前" if last else "—"
            print(f"  {st:10s} {n:>10,} 条  最新 {ago}")
        fp = Path(s.data) / "state" / "feeds.json"
        if fp.exists():
            print("新闻源：")
            for url, st in json.loads(fp.read_text()).items():
                ok = st.get("ok_at")
                bad = st.get("error_at", 0) > (ok or 0)
                print(f"  {'✗' if bad else '✓'} {url[:70]}  {st.get('error', '') if bad else ''}")
        led = Ledger(s.data)
        print(report.card_text(evaluate.scorecard(led.completed(), s)))
        if s["options"]["enabled"]:
            from . import options
            nxt = [e for e in options.event_samples(s) if e.event_at > now][:3]
            print("期权研究：下几个事件 " + "，".join(f"{e.event_type} {e.event_at:%m-%d %H:%M} UTC" for e in nxt))
            print(options.card_text(options.scorecard(s)))
    elif cmd == "backfill":
        all_rows, mds = [], []
        for inst in s.instruments:
            try:
                cs = backfill.fetch_candles(inst["coin"])
                backfill.save(s, inst["coin"], cs)
                rows = backfill.weekend_table(s, cs, inst)
                all_rows += rows
                mds.append(backfill.baseline_report(s, rows, inst["name"]))
            except Exception as e:  # noqa: BLE001
                print("backfill failed", inst["name"], e)
        for p_ in s["market"]["peripheral"]:
            backfill.save(s, p_, backfill.fetch_candles(p_))
        (Path(s.data) / "reports").mkdir(parents=True, exist_ok=True)
        (Path(s.data) / "reports" / "history_weekends.json").write_text(json.dumps(all_rows))
        md = "\n\n".join(mds)
        (Path(s.data) / "reports" / "baseline_history.md").write_text(md)
        print(md[:3000])
        if s["options"]["enabled"]:
            cur = s["options"]["currency"]
            omd = backfill.options_history(s, backfill.fetch_dvol(cur), backfill.fetch_perp(cur))
            (Path(s.data) / "reports" / "options_history.md").write_text(omd)
            print("\n" + omd)
    elif cmd == "report":
        w = last_completed_weekend(now, s)
        print(jobs.job_report(s, w).read_text())
    elif cmd == "note":
        p = Path(s.data) / "state" / "pending_note.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(" ".join(argv[1:]))
        print("已记下，周五收盘时写入实验日志：", p.read_text())
    elif cmd == "review":
        wid, note = argv[1], " ".join(argv[2:])
        Ledger(s.data).append("review", wid, {"note": note})
        print("复盘已写入", wid)
    elif cmd == "adduser":
        import getpass
        from .web import add_user
        name = argv[1] if len(argv) > 1 else input("用户名：").strip()
        pw = getpass.getpass("设置密码（至少 10 位，输入时不显示）：")
        if pw != getpass.getpass("再输一次："):
            print("两次不一致，没有保存。")
            return 1
        add_user(s, name, pw)
        print(f"已创建/更新账号 {name}")
    elif cmd == "link":
        from .web import make_reset_token
        name = argv[1]
        host = os.environ.get("WEB_HOST", "")
        tok = make_reset_token(s, name)
        print(f"在浏览器打开（30 分钟内有效，只能用一次）：\nhttps://{host or '你的网址'}/setpw?token={tok}")
    elif cmd == "deluser":
        from .web import del_user
        print("已删除" if del_user(s, argv[1]) else "没有这个账号")
    elif cmd == "check-llm":
        from . import extract
        rows = rawstore.query(s.data, "news", select="received_at, key, payload", order="received_at DESC")[:5]
        ex = extract.AnthropicExtractor(s["llm"]["model"])
        for r, k, p in rows:
            item = json.loads(p)
            text, user = extract.build_input(item, r, k)
            evs = extract.validate(extract.parse_events(ex(extract.SYSTEM_PROMPT_V01, user)), text, s["market"]["top10"])
            print("—", item["title"])
            for e in evs:
                print("   ", e.get("entities"), e.get("event_type"), "m=", e.get("materiality"), "原句✓" if e["evidence_ok"] else "原句✗")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
