"""Load config.toml and .env; compute the frozen-rules hash (备忘 7.4 第四条：版本可追溯)."""
from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Settings:
    raw: dict
    root: Path
    data: Path

    def __getitem__(self, k):
        return self.raw[k]

    @property
    def instrument(self) -> str:
        return self.raw["market"]["instrument"]


def load(root: Path | None = None) -> Settings:
    root = Path(root or os.environ.get("DESK_ROOT", ROOT))
    load_env(root / ".env")
    with open(root / "config.toml", "rb") as f:
        raw = tomllib.load(f)
    data = Path(os.environ.get("DESK_DATA", root / "data"))
    data.mkdir(parents=True, exist_ok=True)
    return Settings(raw=raw, root=root, data=data)


def rules_hash(root: Path | None = None) -> str:
    """Hash of config + prompt + all code. Changing any of them = a new experiment."""
    root = Path(root or ROOT)
    h = hashlib.sha256()
    files = [root / "config.toml"] + sorted((root / "desk").glob("*.py"))
    files = [f for f in files if f.exists()]
    for p in files:
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]
