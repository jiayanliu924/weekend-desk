"""实验日志（备忘 7.6）。只追加：每个周末依次写入 open → lock → outcome → review。

lock 记录的哈希会推送到手机（带外部时间戳），证明预测在结果出来之前就已锁定。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .rawstore import now_ns


class Ledger:
    def __init__(self, data_dir: Path, name: str = "experiments"):
        self.path = Path(data_dir) / "ledger" / f"{name}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, kind: str, weekend: str, body: dict) -> dict:
        rec = {"kind": kind, "weekend": weekend, "written_at": now_ns(), **body}
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str)
        rec["record_sha256"] = hashlib.sha256(line.encode()).hexdigest()
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        return rec

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        lines = [l for l in self.path.read_text().splitlines() if l.strip()]
        out = []
        for i, l in enumerate(lines):
            try:
                out.append(json.loads(l))
            except ValueError:
                if i != len(lines) - 1:      # 只容忍最后一行写到一半（正在追加）；中间坏行照旧报错
                    raise
        return out

    def weekend(self, wid: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.all():
            if r["weekend"] == wid:
                if r["kind"] == "review":
                    out.setdefault("reviews", []).append(r)
                else:
                    out[r["kind"]] = r  # last one wins only for void/notes; lock is written once
        return out

    def weekends(self) -> list[str]:
        return sorted({r["weekend"] for r in self.all()})

    def completed(self) -> list[dict]:
        """Weekends with both lock and outcome, in order."""
        by: dict[str, dict] = {}
        for r in self.all():          # 只读一次文件（以前每个周末各读一遍整份账本）
            w = by.setdefault(r["weekend"], {})
            if r["kind"] == "review":
                w.setdefault("reviews", []).append(r)
            else:
                w[r["kind"]] = r
        return [{"weekend": wid, **by[wid]} for wid in sorted(by) if "lock" in by[wid] and "outcome" in by[wid]]
