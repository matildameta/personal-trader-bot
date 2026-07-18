"""
Read/write access to the same SQLite file core_engine uses. Kept as an
independent minimal copy (not a shared import) so control_bot can be
deployed/updated separately from core_engine. Schema must stay compatible
with core_engine/src/db.py. Creates the tables it itself writes to
(pending_commands) so it works even if started before core_engine.
"""
import sqlite3
import json
import time
import os
from contextlib import contextmanager

SELF_OWNED_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL
);
"""


class ControlDB:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        with self._conn() as c:
            c.executescript(SELF_OWNED_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- settings (shared with core_engine) ----
    def get_settings(self) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def set_setting(self, key: str, value):
        with self._conn() as c:
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    # ---- bot_messages (track messages the bot itself sent, so we can wipe
    #      the chat on restart -- Telegram's API has no "get history" for bots) ----
    def add_bot_message(self, chat_id: str, message_id: int):
        with self._conn() as c:
            c.execute(
                "INSERT INTO bot_messages (chat_id, message_id) VALUES (?, ?)",
                (str(chat_id), message_id),
            )

    def get_bot_messages(self, chat_id: str) -> list[int]:
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT message_id FROM bot_messages WHERE chat_id = ? ORDER BY id",
                    (str(chat_id),),
                ).fetchall()
            return [r["message_id"] for r in rows]
        except sqlite3.OperationalError:
            return []

    def clear_bot_messages(self, chat_id: str):
        with self._conn() as c:
            c.execute("DELETE FROM bot_messages WHERE chat_id = ?", (str(chat_id),))

    # ---- live_state (read-only here, core_engine writes it every cycle) ----
    def get_live_state(self) -> dict:
        try:
            with self._conn() as c:
                rows = c.execute("SELECT key, value FROM live_state").fetchall()
            return {r["key"]: json.loads(r["value"]) for r in rows}
        except sqlite3.OperationalError:
            return {}  # core_engine hasn't run its first cycle yet

    # ---- trades / logs (read-only here) ----
    def recent_trades(self, limit: int = 10) -> list[dict]:
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []

    def recent_logs(self, limit: int = 15) -> list[dict]:
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT * FROM logs ORDER BY ts DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []

    def today_pnl(self) -> float:
        today_start = time.time() - (time.time() % 86400)
        with self._conn() as c:
            rows = c.execute(
                "SELECT pnl_usd FROM trades WHERE ts >= ? AND status='closed'",
                (today_start,),
            ).fetchall()
        return sum((r["pnl_usd"] or 0) for r in rows)

    def trades_between(self, ts_from: float, ts_to: float) -> list[dict]:
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT * FROM trades WHERE ts >= ? AND ts < ? AND status='closed' ORDER BY ts",
                    (ts_from, ts_to),
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []

    def pnl_summary(self, ts_from: float, ts_to: float | None = None) -> dict:
        ts_to = ts_to or time.time()
        closed = self.trades_between(ts_from, ts_to)
        wins = [t_ for t_ in closed if (t_["pnl_usd"] or 0) > 0]
        losses = [t_ for t_ in closed if (t_["pnl_usd"] or 0) < 0]
        total_pnl = sum((t_["pnl_usd"] or 0) for t_ in closed)
        win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0.0
        best = max(closed, key=lambda t_: t_["pnl_usd"] or 0) if closed else None
        worst = min(closed, key=lambda t_: t_["pnl_usd"] or 0) if closed else None
        avg_win = round(sum((t_["pnl_usd"] or 0) for t_ in wins) / len(wins), 2) if wins else 0.0
        avg_loss = round(sum((t_["pnl_usd"] or 0) for t_ in losses) / len(losses), 2) if losses else 0.0
        return {
            "count": len(closed), "wins": len(wins), "losses": len(losses),
            "win_rate": win_rate, "total_pnl": round(total_pnl, 2),
            "avg_win": avg_win, "avg_loss": avg_loss,
            "best_symbol": best["symbol"] if best else "-",
            "best_pnl": round(best["pnl_usd"], 2) if best else 0.0,
            "worst_symbol": worst["symbol"] if worst else "-",
            "worst_pnl": round(worst["pnl_usd"], 2) if worst else 0.0,
        }

    def consecutive_losses_today(self) -> int:
        today_start = time.time() - (time.time() % 86400)
        with self._conn() as c:
            rows = c.execute(
                "SELECT pnl_usd FROM trades WHERE ts >= ? AND status='closed' ORDER BY ts DESC",
                (today_start,),
            ).fetchall()
        count = 0
        for r in rows:
            if (r["pnl_usd"] or 0) < 0:
                count += 1
            else:
                break
        return count

    # ---- pending_commands (written here, drained by core_engine) ----
    def enqueue_command(self, command_type: str):
        with self._conn() as c:
            c.execute(
                "INSERT INTO pending_commands (ts, type, status) VALUES (?, ?, 'pending')",
                (time.time(), command_type),
            )
