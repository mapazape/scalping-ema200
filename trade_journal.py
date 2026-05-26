import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

JOURNAL_DIR = "journals"


class TradeJournal:
    def __init__(self, journal_dir: str = JOURNAL_DIR) -> None:
        self.journal_dir = journal_dir
        os.makedirs(journal_dir, exist_ok=True)

    def _today_path(self) -> str:
        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        return os.path.join(self.journal_dir, f"trades_{date_str}.jsonl")

    def record(self, trade: dict[str, Any]) -> None:
        entry = {
            "timestamp":        datetime.now(tz=timezone.utc).isoformat(),
            "side":             trade.get("side"),
            "entry":            trade.get("entry_price"),
            "sl":               trade.get("sl"),
            "tp":               trade.get("tp"),
            "exit_price":       trade.get("exit_price"),
            "pnl_usd":         trade.get("pnl_usd"),
            "pnl_pct":         trade.get("pnl_pct"),
            "exit_reason":     trade.get("exit_reason"),
            "ema200_at_entry":  trade.get("ema200_at_entry"),
            "rsi_at_entry":    trade.get("rsi_at_entry"),
            "atr_at_entry":    trade.get("atr_at_entry"),
        }
        path = self._today_path()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info(
            "journal: %s %s pnl=%.4f USD → %s",
            entry["side"], entry["exit_reason"], entry["pnl_usd"] or 0.0, path,
        )
