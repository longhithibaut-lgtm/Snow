"""Per-bot state kept between runs, and the trade journal."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class BotState:
    path: Path
    # "SYMBOL|YYYY-MM-DD" -> TradingAgents rating, so a day's question is paid once.
    llm_cache: dict[str, str] = field(default_factory=dict)
    last_run: str | None = None

    @classmethod
    def load(cls, state_dir: str | Path, bot_name: str) -> BotState:
        path = Path(state_dir) / f"{bot_name}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(path=path, **data)
        return cls(path=path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("path")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)


JOURNAL_FIELDS = ["time", "bot", "mode", "symbol", "side", "qty", "ref_price", "value", "reason", "order_id", "status"]


def journal(log_dir: str | Path, row: dict) -> None:
    path = Path(log_dir) / "trades.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
        if new:
            w.writeheader()
        w.writerow({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), **row})
