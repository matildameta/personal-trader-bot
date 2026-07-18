"""Fetch real trade history directly from Hyperliquid and compute P&L.

Unlike the local `trades` table (which only records what this bot opened),
Hyperliquid's `user_fills` endpoint returns the FULL on-chain history of
every fill for the account — manual trades, exchange-closed SL/TP, and bot
trades alike. This is the authoritative source for P&L reporting.
"""
import time
from collections import defaultdict
from hyperliquid.info import Info
from hyperliquid.utils import constants


def get_fills(account_address: str, network: str = "testnet", since_ms: int | None = None):
    """Return fills (newest first) optionally filtered to those at/after since_ms."""
    api_url = constants.TESTNET_API_URL if network == "testnet" else constants.MAINNET_API_URL
    info = Info(api_url, skip_ws=True)
    try:
        fills = info.user_fills(account_address)
    except Exception as e:
        print(f"[hl_pnl] user_fills failed: {e}")
        return []
    if since_ms is not None:
        fills = [f for f in fills if int(f.get("time", 0)) >= since_ms]
    return fills


def compute_pnl_summary(fills: list[dict]) -> dict:
    """Aggregate closedPnl per coin + overall stats.

    Hyperliquid attributes realized P&L to each closing fill via the
    `closedPnl` field, so we sum that across the window. Win/loss counts
    come from the sign of closedPnl on fills that actually closed a position.
    """
    total_pnl = 0.0
    wins = 0
    losses = 0
    by_symbol = defaultdict(float)
    per_symbol = defaultdict(lambda: {"pnl": 0.0, "wins": 0, "losses": 0, "count": 0})

    for f in fills:
        try:
            pnl = float(f.get("closedPnl", 0) or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        if pnl == 0.0:
            continue  # opening fill, no realized pnl yet
        coin = f.get("coin", "?")
        total_pnl += pnl
        by_symbol[coin] += pnl
        per_symbol[coin]["pnl"] += pnl
        per_symbol[coin]["count"] += 1
        if pnl > 0:
            wins += 1
            per_symbol[coin]["wins"] += 1
        elif pnl < 0:
            losses += 1
            per_symbol[coin]["losses"] += 1

    count = wins + losses
    win_rate = round(wins / count * 100, 1) if count else 0.0
    per_symbol_list = [(k, v) for k, v in per_symbol.items() if v["pnl"] != 0]
    best_sym = max(per_symbol_list, key=lambda kv: kv[1]["pnl"])[0] if per_symbol_list else "-"
    worst_sym = min(per_symbol_list, key=lambda kv: kv[1]["pnl"])[0] if per_symbol_list else "-"

    win_pnls = [p["pnl"] for p in per_symbol.values() if p["pnl"] > 0]
    loss_pnls = [abs(p["pnl"]) for p in per_symbol.values() if p["pnl"] < 0]
    avg_win = round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else 0.0
    avg_loss = round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0.0

    return {
        "count": count,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": round(total_pnl, 2),
        "by_symbol": dict(by_symbol),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "best_symbol": best_sym if per_symbol_list else "-",
        "best_pnl": round(per_symbol[best_sym]["pnl"], 2) if per_symbol_list else 0.0,
        "worst_symbol": worst_sym if per_symbol_list else "-",
        "worst_pnl": round(per_symbol[worst_sym]["pnl"], 2) if per_symbol_list else 0.0,
    }


def pnl_for_period(account_address: str, network: str, period: str) -> dict:
    """period: hourly | daily | weekly | monthly."""
    windows = {
        "hourly": 3600,
        "daily": 86400,
        "weekly": 7 * 86400,
        "monthly": 30 * 86400,
    }
    secs = windows.get(period, 86400)
    since_ms = int((time.time() - secs) * 1000)
    fills = get_fills(account_address, network, since_ms=since_ms)
    return compute_pnl_summary(fills)
