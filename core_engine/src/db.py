"""
Shared SQLite state store.

Both core_engine and control_bot import from this schema (control_bot uses
its own lightweight accessor in shared_db.py, kept schema-compatible).
core_engine reads settings fresh every cycle, writes live_state every
cycle (balance + open positions, so control_bot never needs exchange
keys), and drains pending_commands (currently just "closeall" - the one
action that requires real exchange access and can't be a plain setting).
"""
import sqlite3
import time
import json
import os
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,           -- long / short
    entry_price REAL,
    exit_price REAL,
    size REAL,
    leverage REAL,
    effective_risk_pct REAL,
    stop_loss REAL,
    take_profit REAL,
    status TEXT NOT NULL,         -- open / closed / cancelled
    closed_by TEXT,               -- tp / sl / manual / reversal
    pnl_usd REAL,
    llm_model TEXT,
    llm_confidence REAL,
    reasoning TEXT
);

CREATE TABLE IF NOT EXISTS balance_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,           -- deposit / withdrawal / balance_snapshot
    amount_usd REAL,
    balance_after_usd REAL
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    type TEXT NOT NULL,           -- currently: "closeall"
    status TEXT NOT NULL DEFAULT 'pending'  -- pending / done / failed
);

CREATE TABLE IF NOT EXISTS bot_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL
);
"""

DEFAULTS = {
    "capital_usd": 30,
    "starting_capital_usd": 30,     # baseline for %-based PnL reporting; set once, editable via /setstartcapital
    "max_leverage": 5,
    "risk_per_trade_pct": 1.5,
    "max_daily_loss_pct": 5,
    "max_consecutive_losses": 3,
    "min_notional_usd": 10,
    "language": "en",
    "paused": False,
    "strategy": "balanced",         # analysis persona key, see ai_pipeline.STRATEGIES
                                     # (the AI models themselves are a fixed 4-stage
                                     # pipeline now, see ai_pipeline.STAGE_MODELS — no
                                     # active_model/fallback_model setting anymore)
    "symbols": ["ETH"],             # tradable symbols, editable via /symbols
    "timeframes": ["15m", "1h", "4h"],  # editable via /timeframes
    "report_interval_hours": 24,    # 0 = disabled; auto PnL report cadence, editable via /reportevery
    "sizing_mode": "auto",
    "trade_capital_pct": 50,
}


class SharedDB:
    def __init__(self, path: str, seed_defaults: dict | None = None):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)
        seed = dict(DEFAULTS)
        if seed_defaults:
            seed.update(seed_defaults)
        self._seed_missing(seed)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _seed_missing(self, defaults: dict):
        with self._conn() as c:
            for k, v in defaults.items():
                c.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (k, json.dumps(v)),
                )

    # ---- settings ----
    def get_settings(self) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT key, value FROM settings").fetchall()
        return {k: json.loads(v) for k, v in rows}

    def set_setting(self, key: str, value):
        with self._conn() as c:
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    # ---- trades ----
    def open_trade(self, **kwargs) -> int:
        kwargs.setdefault("ts", time.time())
        kwargs.setdefault("status", "open")
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        with self._conn() as c:
            cur = c.execute(
                f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
                tuple(kwargs.values()),
            )
            return cur.lastrowid

    def close_trade(self, trade_id: int, exit_price: float, pnl_usd: float, closed_by: str = "manual"):
        with self._conn() as c:
            c.execute(
                "UPDATE trades SET status='closed', exit_price=?, pnl_usd=?, closed_by=? WHERE id=?",
                (exit_price, pnl_usd, closed_by, trade_id),
            )

    def open_trades_for_symbol(self, symbol: str) -> list[dict]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM trades WHERE symbol=? AND status='open'", (symbol,)
            ).fetchall()
        return [dict(r) for r in rows]

    def open_trades(self) -> list[dict]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM trades WHERE status='open'"
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_trades(self, limit: int = 10) -> list[dict]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def trades_since(self, ts_from: float) -> list[dict]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM trades WHERE ts >= ? ORDER BY ts", (ts_from,)
            ).fetchall()
        return [dict(r) for r in rows]

    def trades_between(self, ts_from: float, ts_to: float) -> list[dict]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM trades WHERE ts >= ? AND ts < ? AND status='closed' ORDER BY ts",
                (ts_from, ts_to),
            ).fetchall()
        return [dict(r) for r in rows]

    def pnl_summary(self, ts_from: float, ts_to: float | None = None) -> dict:
        """Aggregate stats for a period, used for daily/weekly/monthly reports."""
        ts_to = ts_to or time.time()
        trades = self.trades_between(ts_from, ts_to)
        closed = [t_ for t_ in trades if t_.get("pnl_usd") is not None]
        wins = [t_ for t_ in closed if (t_["pnl_usd"] or 0) > 0]
        losses = [t_ for t_ in closed if (t_["pnl_usd"] or 0) < 0]
        total_pnl = sum((t_["pnl_usd"] or 0) for t_ in closed)
        win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0.0
        best = max(closed, key=lambda t_: t_["pnl_usd"] or 0) if closed else None
        worst = min(closed, key=lambda t_: t_["pnl_usd"] or 0) if closed else None
        avg_win = round(sum((t_["pnl_usd"] or 0) for t_ in wins) / len(wins), 2) if wins else 0.0
        avg_loss = round(sum((t_["pnl_usd"] or 0) for t_ in losses) / len(losses), 2) if losses else 0.0
        return {
            "count": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "total_pnl": round(total_pnl, 2),
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "best_symbol": best["symbol"] if best else "-",
            "best_pnl": round(best["pnl_usd"], 2) if best else 0.0,
            "worst_symbol": worst["symbol"] if worst else "-",
            "worst_pnl": round(worst["pnl_usd"], 2) if worst else 0.0,
        }

    # ---- balance events ----
    def log_balance_event(self, kind: str, amount_usd: float, balance_after_usd: float):
        with self._conn() as c:
            c.execute(
                "INSERT INTO balance_events (ts, kind, amount_usd, balance_after_usd) "
                "VALUES (?, ?, ?, ?)",
                (time.time(), kind, amount_usd, balance_after_usd),
            )

    # ---- logs ----
    def log(self, level: str, message: str):
        with self._conn() as c:
            c.execute(
                "INSERT INTO logs (ts, level, message) VALUES (?, ?, ?)",
                (time.time(), level, message),
            )

    def recent_logs(self, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM logs ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- live_state (written by core_engine every cycle, read-only for control_bot) ----
    def set_live_state(self, key: str, value):
        with self._conn() as c:
            c.execute(
                "INSERT INTO live_state (key, value, updated_ts) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
                (key, json.dumps(value), time.time()),
            )

    def get_live_state(self) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT key, value FROM live_state").fetchall()
        return {k: json.loads(v) for k, v in rows}

    # ---- pending_commands (written by control_bot, drained by core_engine) ----
    def get_pending_commands(self) -> list[dict]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM pending_commands WHERE status='pending' ORDER BY ts"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_command(self, command_id: int, status: str):
        with self._conn() as c:
            c.execute("UPDATE pending_commands SET status=? WHERE id=?", (status, command_id))
