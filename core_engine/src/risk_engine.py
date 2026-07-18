"""
All money-math lives here, as plain deterministic Python. The LLM never
sets size, leverage, or whether the kill-switch trips -- it only proposes
a direction/confidence/SL-TP percentage, which this module treats as
input to validate and clamp, never as an instruction to trust blindly.

ADDED vs original:
  - atr_floor_stop(): stops noise stop-outs by refusing an SL tighter than
    a multiple of ATR, regardless of what the LLM suggested.
  - drawdown_throttle(): cuts risk-per-trade automatically as the day/week
    goes worse, instead of a flat risk_pct all the time.
  - volatility_adjusted_leverage(): caps leverage lower when ATR% is high,
    instead of always allowing max_leverage.
"""
from dataclasses import dataclass


@dataclass
class PositionPlan:
    size_usd: float
    leverage: float
    stop_loss_price: float
    take_profit_price: float
    effective_risk_pct: float
    notional_usd: float
    rejected: bool = False
    rejection_reason: str = ""


def atr_floor_stop(
    *, side: str, stop_loss_pct: float, take_profit_pct: float,
    atr_pct: float, min_atr_multiple: float = 1.2, min_rr: float = 1.3,
) -> tuple:
    """If the LLM's suggested SL is tighter than `min_atr_multiple` * ATR%,
    it's very likely to get stopped out by ordinary noise rather than by
    being wrong. Widen the SL to the ATR floor and rescale TP to preserve
    at least `min_rr`. Returns (stop_loss_pct, take_profit_pct), unchanged
    if atr_pct is unavailable or the original SL already clears the floor.
    """
    if not atr_pct or atr_pct <= 0 or not stop_loss_pct or stop_loss_pct <= 0:
        return stop_loss_pct, take_profit_pct

    floor = round(atr_pct * min_atr_multiple, 3)
    if stop_loss_pct >= floor:
        return stop_loss_pct, take_profit_pct

    new_sl = floor
    # keep the TP's original R-multiple if it already implied >= min_rr,
    # otherwise stretch TP to satisfy min_rr off the new (wider) SL.
    implied_rr = take_profit_pct / stop_loss_pct if stop_loss_pct else min_rr
    rr = max(implied_rr, min_rr)
    new_tp = round(new_sl * rr, 3)
    return new_sl, new_tp


def drawdown_throttle(
    *, base_risk_pct: float, pnl_today_usd: float, capital_usd: float,
    consecutive_losses: int,
) -> float:
    """Cuts risk-per-trade as drawdown deepens, instead of a flat risk_pct
    right up until the kill switch trips. Purely defensive -- it can only
    reduce risk, never increase it above base_risk_pct."""
    if capital_usd <= 0:
        return base_risk_pct

    loss_pct_today = max(0.0, -pnl_today_usd / capital_usd * 100)
    risk = base_risk_pct

    if loss_pct_today >= 1.0:
        risk *= 0.75
    if loss_pct_today >= 2.5:
        risk *= 0.6
    if loss_pct_today >= 4.0:
        risk *= 0.4

    if consecutive_losses >= 2:
        risk *= 0.8
    if consecutive_losses >= 3:
        risk *= 0.6

    return round(max(risk, base_risk_pct * 0.2), 4)


def volatility_adjusted_leverage(*, max_leverage: int, atr_pct: float) -> int:
    """Lower the leverage ceiling when ATR% is elevated, instead of always
    allowing max_leverage regardless of how wild the market currently is."""
    if not atr_pct or atr_pct <= 0:
        return max_leverage
    if atr_pct >= 2.5:
        return max(1, int(max_leverage * 0.4))
    if atr_pct >= 1.5:
        return max(1, int(max_leverage * 0.6))
    if atr_pct >= 1.0:
        return max(1, int(max_leverage * 0.8))
    return max_leverage


def plan_position(
    *,
    capital_usd: float,
    entry_price: float,
    side: str,                     # "long" or "short"
    stop_loss_pct: float,          # from LLM, e.g. 1.5 meaning 1.5%
    take_profit_pct: float,
    max_leverage: int,
    risk_per_trade_pct: float,
    min_notional_usd: float,
) -> PositionPlan:
    """
    Sizing logic (unchanged from original):
    1. Ideal risk amount = capital * risk_per_trade_pct / 100
    2. Stop distance (in price %) determines how much notional corresponds
       to that risk amount: notional = risk_amount / (stop_loss_pct / 100)
    3. Clamp notional so leverage never exceeds max_leverage
       (notional <= capital * max_leverage)
    4. If the resulting notional is below the exchange minimum, we bump it
       up to the minimum and report the *effective* (higher) risk % -- we
       never silently pretend the real risk was smaller than it was.

    Callers should now pass already-throttled risk_per_trade_pct (see
    drawdown_throttle), an already-capped max_leverage (see
    volatility_adjusted_leverage), and an already-ATR-floored stop_loss_pct
    (see atr_floor_stop) -- this function stays a pure, dumb calculator on
    purpose so it stays easy to unit-test.
    """
    if stop_loss_pct <= 0:
        return PositionPlan(0, 0, 0, 0, 0, 0, rejected=True, rejection_reason="invalid stop_loss_pct")

    risk_amount_usd = capital_usd * (risk_per_trade_pct / 100)
    ideal_notional = risk_amount_usd / (stop_loss_pct / 100)

    max_notional_by_leverage = capital_usd * max_leverage
    notional = min(ideal_notional, max_notional_by_leverage)

    bumped = False
    if notional < min_notional_usd:
        notional = min_notional_usd
        bumped = True

    if notional > max_notional_by_leverage:
        return PositionPlan(
            0, 0, 0, 0, 0, 0, rejected=True,
            rejection_reason=(
                f"even the minimum order size (${min_notional_usd}) would exceed "
                f"max_leverage ({max_leverage}x) given capital ${capital_usd}"
            ),
        )

    leverage = round(notional / capital_usd, 2)
    effective_risk_pct = round((notional * (stop_loss_pct / 100)) / capital_usd * 100, 2)

    if side == "long":
        sl_price = entry_price * (1 - stop_loss_pct / 100)
        tp_price = entry_price * (1 + take_profit_pct / 100)
    else:
        sl_price = entry_price * (1 + stop_loss_pct / 100)
        tp_price = entry_price * (1 - take_profit_pct / 100)

    return PositionPlan(
        size_usd=round(notional / entry_price, 6),  # in base asset units
        leverage=leverage,
        stop_loss_price=round(sl_price, 4),
        take_profit_price=round(tp_price, 4),
        effective_risk_pct=effective_risk_pct,
        notional_usd=round(notional, 2),
        rejected=False,
        rejection_reason="min order size forced higher risk than requested" if bumped else "",
    )


class KillSwitch:
    """Tracks daily PnL and consecutive losses; trips = pause trading."""

    def __init__(self, max_daily_loss_pct: float, max_consecutive_losses: int):
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_consecutive_losses = max_consecutive_losses

    def check(self, *, capital_usd: float, pnl_today_usd: float, consecutive_losses: int) -> str | None:
        """Returns a trip reason string, or None if OK to keep trading."""
        if capital_usd <= 0:
            return "capital is zero or negative"
        loss_pct_today = -pnl_today_usd / capital_usd * 100
        if loss_pct_today >= self.max_daily_loss_pct:
            return f"daily loss {loss_pct_today:.2f}% >= limit {self.max_daily_loss_pct}%"
        if consecutive_losses >= self.max_consecutive_losses:
            return f"{consecutive_losses} consecutive losing trades >= limit {self.max_consecutive_losses}"
        return None
