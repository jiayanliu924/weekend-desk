"""原始层（备忘 7.2）：只追加不修改；每条记录收到时间；来源与内容哈希；Parquet 按日分区。

Layout: data/raw/<stream>/date=YYYY-MM-DD/part-<first_received_ns>-<n>.parquet
Columns: received_at (int64 ns UTC), source (str), key (str), payload (str, original JSON/text), sha256 (str)
Part files are written once and never rewritten.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

SCHEMA = pa.schema([
    ("received_at", pa.int64()),
    ("source", pa.string()),
    ("key", pa.string()),
    ("payload", pa.string()),
    ("sha256", pa.string()),
])


def now_ns() -> int:
    return time.time_ns()


def sha(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()


class RawWriter:
    def __init__(self, data_dir: Path, stream: str):
        self.dir = Path(data_dir) / "raw" / stream
        self.stream = stream
        self.buf: list[tuple] = []
        self.lock = threading.Lock()
        self.count = 0

    def add(self, source: str, key: str, payload, received_at: int | None = None) -> None:
        if not isinstance(payload, str):
            payload = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        r = received_at or now_ns()
        with self.lock:
            self.buf.append((r, source, key, payload, sha(payload)))

    def flush(self) -> int:
        with self.lock:
            rows, self.buf = self.buf, []
        if not rows:
            return 0
        # split by UTC date of received_at
        by_day: dict[str, list[tuple]] = {}
        for row in rows:
            d = datetime.fromtimestamp(row[0] / 1e9, tz=timezone.utc).strftime("%Y-%m-%d")
            by_day.setdefault(d, []).append(row)
        for d, rs in by_day.items():
            part_dir = self.dir / f"date={d}"
            part_dir.mkdir(parents=True, exist_ok=True)
            path = part_dir / f"part-{rs[0][0]}-{len(rs)}.parquet"
            cols = list(zip(*rs))
            table = pa.table({n: list(c) for n, c in zip(SCHEMA.names, cols)}, schema=SCHEMA)
            tmp = path.with_suffix(".tmp")
            pq.write_table(table, tmp, compression="zstd")
            tmp.rename(path)  # atomic: a part either fully exists or not at all
        self.count += len(rows)
        return len(rows)


def glob(data_dir: Path, stream: str) -> str:
    return str(Path(data_dir) / "raw" / stream / "date=*" / "*.parquet")


def has_stream(data_dir: Path, stream: str) -> bool:
    return any((Path(data_dir) / "raw" / stream).glob("date=*/*.parquet"))


def files_between(data_dir: Path, stream: str, a_ns: int, b_ns: int) -> list[str]:
    """只列 received_at 落在 [a_ns, b_ns) 可能出现的日期分区（分区按 received_at 的 UTC 日期切）。"""
    from datetime import timedelta
    d0 = datetime.fromtimestamp(a_ns // 10**9, tz=timezone.utc).date()
    d1 = datetime.fromtimestamp(max(a_ns, b_ns) // 10**9, tz=timezone.utc).date()
    out, d = [], d0
    base = Path(data_dir) / "raw" / stream
    while d <= d1:
        out += sorted(str(p) for p in (base / f"date={d.isoformat()}").glob("*.parquet"))
        d += timedelta(days=1)
    return out


def query(data_dir: Path, stream: str, sql_where: str = "TRUE", select: str = "*",
          order: str = "received_at", a_ns: int | None = None, b_ns: int | None = None) -> list[tuple]:
    """a_ns/b_ns 给了就只读这段时间的日期分区（2026-10-18：以前每次都扫全部历史分区，锁一个合约要 ~1 分钟）。
    调用方的 sql_where 仍须自己写 received_at 条件——分区裁剪只是少读文件，不改变结果。"""
    if not has_stream(data_dir, stream):
        return []
    if a_ns is not None and b_ns is not None:
        files = files_between(data_dir, stream, a_ns, b_ns)
        if not files:
            return []
        src = "[" + ", ".join("'" + f.replace("'", "''") + "'" for f in files) + "]"
    else:
        src = f"'{glob(data_dir, stream)}'"
    con = duckdb.connect()
    try:
        con.execute("SET memory_limit='150MB'")
        con.execute("SET threads=1")
        q = f"SELECT {select} FROM read_parquet({src}, hive_partitioning=true) WHERE {sql_where} ORDER BY {order}"
        return con.execute(q).fetchall()
    finally:
        con.close()


def verify(data_dir: Path, stream: str) -> dict:
    """Recompute content hashes (备忘 7.2：每日自动核对)."""
    rows = query(data_dir, stream, select="payload, sha256")
    bad = sum(1 for p, s in rows if sha(p) != s)
    return {"stream": stream, "rows": len(rows), "hash_mismatch": bad}
