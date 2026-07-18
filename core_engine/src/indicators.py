"""
Computes indicators as plain numbers and formats a compact text summary
for the LLM.

CHANGES vs original:
  - candles_to_df() now keeps the "t" (open time) column so VWAP and the
    unclosed-candle guard in main.py have something to anchor on.
  - compute_indicators() now ALSO returns fields the rule_engine needs and
    the LLM never reliably infers on its own from raw numbers:
      atr_pct, bb_width_pct, bb_width_percentile   -> volatility regime
      vwap20 / vwap50                              -> mean-reversion anchor
      swing_high / swing_low, structure            -> BOS / CHOCH (rule-based
                                                       market structure, not
                                                       LLM-guessed)
      liquidity_sweep                              -> wick-based stop-hunt
                                                       detector
  - NOTE on scope: true CVD / order-flow / delta needs an L2 order-book or
    trade-tape feed, which klines do not provide. liquidity_sweep below is
    a wick-based proxy, not real order flow -- said explicitly so nobody
    mistakes it for the real thing.
"""
import pandas as pd
import pandas_ta as ta


def compute_indicators(df: pd.DataFrame) -> dict:
    """df must have columns: open, high, low, close, volume (chronological
    order), and SHOULD already have the currently-forming candle dropped
    by the caller (see main.py's drop_unclosed_candle) -- this function
    does not know "now", so it trusts whatever is in the last row."""
    out = {}
    df = df.copy()

    df["rsi14"] = ta.rsi(df["close"], length=14)
    macd = ta.macd(df["close"])
    df["macd"] = macd.iloc[:, 0]
    df["macd_signal"] = macd.iloc[:, 2]
    df["ema20"] = ta.ema(df["close"], length=20)
    df["ema50"] = ta.ema(df["close"], length=50)
    bb = ta.bbands(df["close"], length=20)
    df["bb_upper"] = bb.iloc[:, 2]
    df["bb_lower"] = bb.iloc[:, 0]
    df["bb_mid"] = bb.iloc[:, 1]
    df["atr14"] = ta.atr(df["high"], df["low"], df["close"], length=14)

    adx = ta.adx(df["high"], df["low"], df["close"], length=14)
    if adx is not None and not adx.empty:
        df["adx14"] = adx.iloc[:, 0]
        df["di_plus"] = adx.iloc[:, 1]
        df["di_minus"] = adx.iloc[:, 2]
    else:
        df["adx14"] = df["di_plus"] = df["di_minus"] = None

    stochrsi = ta.stochrsi(df["close"], length=14)
    if stochrsi is not None and not stochrsi.empty:
        df["stochrsi_k"] = stochrsi.iloc[:, 0]
        df["stochrsi_d"] = stochrsi.iloc[:, 1]
    else:
        df["stochrsi_k"] = df["stochrsi_d"] = None

    df["vol_avg20"] = df["volume"].rolling(20).mean()

    # --- volatility regime fields --------------------------------------
    df["atr_pct"] = df["atr14"] / df["close"] * 100
    df["bb_width_pct"] = (df["bb_upper"] - df["bb_lower"]) / df["close"] * 100
    # percentile rank of the *current* bb width vs its own trailing 100 bars
    # (0 = tightest squeeze in the lookback, 100 = widest expansion)
    df["bb_width_percentile"] = df["bb_width_pct"].rolling(100, min_periods=30).apply(
        lambda s: (s.rank(pct=True).iloc[-1] * 100) if len(s.dropna()) > 5 else float("nan"),
        raw=False,
    )

    # --- rolling VWAP (typical price, volume weighted) -----------------
    # Not a true session-anchored VWAP (klines here aren't day-aligned),
    # but a valid rolling volume-weighted mean-reversion reference.
    typical = (df["high"] + df["low"] + df["close"]) / 3
    tp_vol = typical * df["volume"]
    df["vwap20"] = tp_vol.rolling(20).sum() / df["volume"].rolling(20).sum()
    df["vwap50"] = tp_vol.rolling(50).sum() / df["volume"].rolling(50).sum()

    last = df.iloc[-1]

    def _r(col, ndig=2):
        v = last.get(col)
        return round(float(v), ndig) if pd.notna(v) else None

    out["close"] = float(last["close"])
    out["rsi14"] = _r("rsi14")
    out["macd"] = _r("macd", 4)
    out["macd_signal"] = _r("macd_signal", 4)
    out["ema20"] = _r("ema20")
    out["ema50"] = _r("ema50")
    out["bb_upper"] = _r("bb_upper")
    out["bb_lower"] = _r("bb_lower")
    out["atr14"] = _r("atr14", 4)
    out["adx14"] = _r("adx14")
    out["di_plus"] = _r("di_plus")
    out["di_minus"] = _r("di_minus")
    out["stochrsi_k"] = _r("stochrsi_k")
    out["stochrsi_d"] = _r("stochrsi_d")
    out["atr_pct"] = _r("atr_pct", 3)
    out["bb_width_pct"] = _r("bb_width_pct", 3)
    out["bb_width_percentile"] = _r("bb_width_percentile", 1)
    out["vwap20"] = _r("vwap20")
    out["vwap50"] = _r("vwap50")

    vol_avg = last.get("vol_avg20")
    vol = last.get("volume")
    if pd.notna(vol_avg) and vol_avg and pd.notna(vol):
        out["vol_ratio"] = round(float(vol) / float(vol_avg), 2)
    else:
        out["vol_ratio"] = None

    if out["ema20"] is not None and out["ema50"] is not None:
        if out["close"] > out["ema20"] > out["ema50"]:
            out["trend"] = "uptrend"
        elif out["close"] < out["ema20"] < out["ema50"]:
            out["trend"] = "downtrend"
        else:
            out["trend"] = "mixed/ranging"
    else:
        out["trend"] = "unknown"

    # --- market structure: swing highs/lows -> BOS / CHOCH --------------
    struct = _detect_structure(df)
    out.update(struct)

    # --- liquidity sweep (wick-based stop-hunt proxy) --------------------
    out["liquidity_sweep"] = _detect_liquidity_sweep(df, struct)

    return out


def _find_swings(df: pd.DataFrame, left: int = 3, right: int = 3):
    """Fractal swing points. Only returns CONFIRMED swings (needs `right`
    bars after the pivot), so nothing here peeks into the future relative
    to the pivot bar itself."""
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    swing_highs, swing_lows = [], []
    for i in range(left, n - right):
        h_window = highs[i - left:i + right + 1]
        if highs[i] == h_window.max() and (h_window == highs[i]).sum() == 1:
            swing_highs.append((i, float(highs[i])))
        l_window = lows[i - left:i + right + 1]
        if lows[i] == l_window.min() and (l_window == lows[i]).sum() == 1:
            swing_lows.append((i, float(lows[i])))
    return swing_highs, swing_lows


def _detect_structure(df: pd.DataFrame) -> dict:
    """Simplified, honest BOS/CHOCH:
    - structure_trend: 'bullish' if the last two confirmed swings show
      higher-highs & higher-lows, 'bearish' if lower-highs & lower-lows,
      else 'mixed'.
    - structure: 'bullish_bos' / 'bearish_bos' when the latest CLOSE breaks
      the last swing extreme in the direction of structure_trend (trend
      continuation), 'bullish_choch' / 'bearish_choch' when it breaks the
      extreme AGAINST structure_trend (first sign of reversal), else 'none'.
    """
    swing_highs, swing_lows = _find_swings(df)
    out = {
        "swing_high": None, "swing_low": None,
        "structure_trend": "unknown", "structure": "none",
    }
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return out

    last_high, prev_high = swing_highs[-1][1], swing_highs[-2][1]
    last_low, prev_low = swing_lows[-1][1], swing_lows[-2][1]
    out["swing_high"] = round(last_high, 4)
    out["swing_low"] = round(last_low, 4)

    if last_high > prev_high and last_low > prev_low:
        trend = "bullish"
    elif last_high < prev_high and last_low < prev_low:
        trend = "bearish"
    else:
        trend = "mixed"
    out["structure_trend"] = trend

    close = float(df["close"].iloc[-1])
    if close > last_high:
        out["structure"] = "bullish_bos" if trend == "bullish" else "bullish_choch"
    elif close < last_low:
        out["structure"] = "bearish_bos" if trend == "bearish" else "bearish_choch"
    else:
        out["structure"] = "none"
    return out


def _detect_liquidity_sweep(df: pd.DataFrame, struct: dict) -> str:
    """Wick-based proxy for a stop-hunt: last CLOSED candle's wick pierces
    the prior swing low/high but the candle CLOSES back inside range.
    'bullish' = sweep of sell-side liquidity below the swing low (often
    precedes a bounce). 'bearish' = sweep above the swing high. This is a
    heuristic on OHLC only -- real order-flow/CVD needs an L2 feed, which
    is out of scope here."""
    if struct["swing_low"] is None or struct["swing_high"] is None:
        return "none"
    last = df.iloc[-1]
    low, high, close = float(last["low"]), float(last["high"]), float(last["close"])
    if low < struct["swing_low"] and close > struct["swing_low"]:
        return "bullish"
    if high > struct["swing_high"] and close < struct["swing_high"]:
        return "bearish"
    return "none"


def format_multi_timeframe_summary(symbol: str, per_timeframe: dict) -> str:
    """per_timeframe: {"15m": {...indicators...}, "1h": {...}, "4h": {...}}"""
    lines = []
    for tf, ind in per_timeframe.items():
        lines.append(
            f"[{tf}] close={ind['close']} trend={ind['trend']} "
            f"rsi14={ind['rsi14']} stochrsi=({ind['stochrsi_k']},{ind['stochrsi_d']}) "
            f"macd={ind['macd']}/{ind['macd_signal']} "
            f"ema20={ind['ema20']} ema50={ind['ema50']} "
            f"bb=({ind['bb_lower']},{ind['bb_upper']}) bb_width_pctl={ind['bb_width_percentile']} "
            f"atr14={ind['atr14']} atr_pct={ind['atr_pct']} "
            f"adx14={ind['adx14']} di+={ind['di_plus']} di-={ind['di_minus']} "
            f"vwap20={ind['vwap20']} vol_ratio={ind['vol_ratio']} "
            f"structure={ind['structure']}({ind['structure_trend']}) "
            f"swing=({ind['swing_low']},{ind['swing_high']}) "
            f"liquidity_sweep={ind['liquidity_sweep']}"
        )
    return "\n".join(lines)
