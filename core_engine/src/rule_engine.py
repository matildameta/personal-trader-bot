"""
Deterministic gate between "the LLM said long/short" and "an order is
actually placed". This is the piece that was missing: everything here is
plain Python over numbers already computed in indicators.py -- no model
call, no prompt, nothing probabilistic. It can REJECT an LLM signal, but
it can never invent a signal the LLM didn't propose (the LLM still owns
direction; this module owns "is this actually a good idea right now").

Design intent, matching the problems you listed:
  - real trend filter (multi-timeframe EMA/ADX alignment)
  - market regime detection (trending / ranging / volatile-chop)
  - volatility filter (ATR% band, BB-width percentile)
  - volume filter (dead volume / spike requirement, persona-dependent)
  - real multi-timeframe consensus (independent of the LLM's own read)
  - structure filter (BOS/CHOCH from indicators.py)
  - minimum RR filter + simple expected-value sanity check
  - one shared confidence threshold, raised and made persona-aware

None of this guarantees a higher win rate -- markets aren't stationary and
no rule set is risk-free. What it does is stop the two structurally bad
trades your logs almost certainly show a lot of: (a) taking the LLM's word
in a dead/choppy market with no real edge, and (b) taking trades whose
SL/TP geometry can't be profitable even at a coin-flip win rate.
"""
from dataclasses import dataclass, field


# Per-persona knobs. Personas not listed fall back to "balanced".
PERSONA_RULES = {
    "conservative": dict(min_confidence=0.68, min_rr=1.6, min_agree_tfs=3,
                          min_vol_ratio=0.7, require_structure_align=True),
    "balanced":     dict(min_confidence=0.60, min_rr=1.4, min_agree_tfs=2,
                          min_vol_ratio=0.6, require_structure_align=False),
    "aggressive":   dict(min_confidence=0.55, min_rr=1.2, min_agree_tfs=2,
                          min_vol_ratio=0.5, require_structure_align=False),
    "scalper":      dict(min_confidence=0.58, min_rr=1.1, min_agree_tfs=1,
                          min_vol_ratio=1.1, require_structure_align=False),
    "trend_follower": dict(min_confidence=0.62, min_rr=1.8, min_agree_tfs=3,
                            min_vol_ratio=0.6, require_structure_align=True),
    "mean_reversion": dict(min_confidence=0.60, min_rr=1.3, min_agree_tfs=2,
                            min_vol_ratio=0.5, require_structure_align=False),
    "swing":        dict(min_confidence=0.62, min_rr=1.8, min_agree_tfs=2,
                          min_vol_ratio=0.5, require_structure_align=True),
    "gemini":       dict(min_confidence=0.60, min_rr=1.4, min_agree_tfs=2,
                          min_vol_ratio=0.6, require_structure_align=False),
    "god_mode":     dict(min_confidence=0.65, min_rr=1.5, min_agree_tfs=2,
                          min_vol_ratio=0.6, require_structure_align=False),
}

# ATR% band: below MIN = too dead to pay funding+fees+slippage on; above
# MAX = news-spike / illiquid-wick conditions where SL gets sniped by noise.
DEFAULT_ATR_PCT_MIN = 0.08
DEFAULT_ATR_PCT_MAX = 3.0
# bb_width_percentile below this = squeeze / chop, most breakouts here fail.
DEFAULT_CHOP_BB_PERCENTILE = 15


@dataclass
class RuleDecision:
    approved: bool
    reason: str
    regime: str = "unknown"
    consensus_score: float = 0.0        # -100..100, sign = direction, |x| = strength
    agree_timeframes: int = 0
    total_timeframes: int = 0
    blended_confidence: float = 0.0
    notes: list = field(default_factory=list)


def _tf_directional_score(ind: dict) -> float:
    """Score one timeframe's indicators, independent of the LLM, into a
    single -100..100 number. Purely rule-based so it can be compared
    against what the LLM claims."""
    score = 0.0
    votes = 0

    ema20, ema50, close = ind.get("ema20"), ind.get("ema50"), ind.get("close")
    if ema20 is not None and ema50 is not None and close is not None:
        votes += 1
        if close > ema20 > ema50:
            score += 25
        elif close < ema20 < ema50:
            score -= 25
        elif close > ema20:
            score += 10
        elif close < ema20:
            score -= 10

    macd, macd_sig = ind.get("macd"), ind.get("macd_signal")
    if macd is not None and macd_sig is not None:
        votes += 1
        score += 15 if macd > macd_sig else -15

    rsi = ind.get("rsi14")
    if rsi is not None:
        votes += 1
        if rsi > 55:
            score += 10
        elif rsi < 45:
            score -= 10

    adx, di_plus, di_minus = ind.get("adx14"), ind.get("di_plus"), ind.get("di_minus")
    if adx is not None and di_plus is not None and di_minus is not None and adx >= 18:
        votes += 1
        score += 20 if di_plus > di_minus else -20

    structure = ind.get("structure")
    if structure and structure != "none":
        votes += 1
        if "bullish" in structure:
            score += 20
        elif "bearish" in structure:
            score -= 20

    sweep = ind.get("liquidity_sweep")
    if sweep == "bullish":
        score += 10
    elif sweep == "bearish":
        score -= 10

    return max(-100.0, min(100.0, score))


def detect_regime(htf_ind: dict) -> str:
    """Regime from the HIGHEST timeframe available (trend context should
    come from the big picture, not the entry timeframe)."""
    adx = htf_ind.get("adx14")
    bbw_pctl = htf_ind.get("bb_width_percentile")
    atr_pct = htf_ind.get("atr_pct")

    if atr_pct is not None and atr_pct > DEFAULT_ATR_PCT_MAX:
        return "volatile_chop"
    if adx is not None and adx >= 25:
        return "trending"
    if bbw_pctl is not None and bbw_pctl <= DEFAULT_CHOP_BB_PERCENTILE:
        return "ranging_squeeze"
    if adx is not None and adx < 18:
        return "ranging"
    return "mixed"


def multi_timeframe_consensus(per_tf_indicators: dict, timeframe_weights: dict = None) -> tuple:
    """Weighted vote across all fetched timeframes. Higher timeframes get
    more weight by default (trend context matters more than entry noise).
    Returns (consensus_score -100..100, per_tf_scores dict)."""
    tfs = list(per_tf_indicators.keys())
    if timeframe_weights is None:
        # crude default: later-listed (assumed higher) timeframes weighted more
        n = len(tfs)
        timeframe_weights = {tf: (i + 1) for i, tf in enumerate(tfs)} if n else {}

    per_tf_scores = {tf: _tf_directional_score(ind) for tf, ind in per_tf_indicators.items()}
    total_w = sum(timeframe_weights.get(tf, 1) for tf in tfs) or 1
    consensus = sum(per_tf_scores[tf] * timeframe_weights.get(tf, 1) for tf in tfs) / total_w
    return round(consensus, 1), per_tf_scores


def check_min_rr(stop_loss_pct: float, take_profit_pct: float, min_rr: float) -> bool:
    if not stop_loss_pct or stop_loss_pct <= 0:
        return False
    rr = take_profit_pct / stop_loss_pct
    return rr >= min_rr


def evaluate_trade(
    *,
    persona: str,
    side: str,                      # "long" | "short"
    llm_confidence: float,
    per_tf_indicators: dict,        # {"15m": {...}, "1h": {...}, "4h": {...}}
    entry_tf: str,                  # timeframe used for entry timing (lowest fetched)
    htf: str,                       # timeframe used for trend context (highest fetched)
    stop_loss_pct: float,
    take_profit_pct: float,
    override_rules: dict = None,
) -> RuleDecision:
    """The single gate main.py should call before sizing/executing a trade.
    Rejects with a specific reason; never silently mutates side/confidence
    without saying so in `notes`."""
    rules = dict(PERSONA_RULES.get(persona, PERSONA_RULES["balanced"]))
    if override_rules:
        rules.update(override_rules)

    notes = []
    entry_ind = per_tf_indicators.get(entry_tf, {})
    htf_ind = per_tf_indicators.get(htf, entry_ind)

    regime = detect_regime(htf_ind)
    consensus, per_tf_scores = multi_timeframe_consensus(per_tf_indicators)

    sign = 1 if side == "long" else -1
    agree = sum(1 for s in per_tf_scores.values() if s * sign > 5)
    total = len(per_tf_scores)

    decision = RuleDecision(
        approved=False, reason="", regime=regime, consensus_score=consensus,
        agree_timeframes=agree, total_timeframes=total,
        blended_confidence=llm_confidence, notes=notes,
    )

    # 1) regime filter -- don't trade chop/squeeze as if it were a trend
    if regime in ("ranging_squeeze",):
        decision.reason = f"regime={regime}: squeeze/no-range-expansion, skipping"
        return decision
    if regime == "volatile_chop":
        decision.reason = f"regime={regime}: ATR% spike beyond {DEFAULT_ATR_PCT_MAX}%, likely noise/news wick"
        return decision

    # 2) trend filter -- htf structure/EMA must not flatly oppose the trade
    #    (skip this for mean_reversion, which intentionally fades extremes)
    if persona != "mean_reversion":
        htf_score = per_tf_scores.get(htf, 0)
        if htf_score * sign < -15:
            decision.reason = (
                f"trend filter: higher timeframe ({htf}) score={htf_score} "
                f"opposes {side}"
            )
            return decision

    # 3) rule-based multi-timeframe consensus must agree with the LLM's side
    if consensus * sign < 5:
        decision.reason = f"consensus filter: rule-based consensus={consensus} does not support {side}"
        return decision
    if agree < rules["min_agree_tfs"]:
        decision.reason = (
            f"consensus filter: only {agree}/{total} timeframes agree with {side} "
            f"(need >= {rules['min_agree_tfs']})"
        )
        return decision

    # 4) structure alignment (personas that require it, e.g. trend_follower/swing)
    if rules.get("require_structure_align"):
        struct = htf_ind.get("structure", "none")
        if struct == "none":
            decision.reason = f"structure filter: no confirmed BOS/CHOCH on {htf}, persona requires structural confirmation"
            return decision
        if ("bullish" in struct and side != "long") or ("bearish" in struct and side != "short"):
            decision.reason = f"structure filter: {htf} structure={struct} conflicts with {side}"
            return decision

    # 5) volatility filter -- avoid dead markets (can't cover fees/funding/slippage)
    atr_pct = entry_ind.get("atr_pct")
    if atr_pct is not None and atr_pct < DEFAULT_ATR_PCT_MIN:
        decision.reason = f"volatility filter: atr_pct={atr_pct}% on {entry_tf} too low, likely dead market"
        return decision

    # 6) volume filter -- persona-dependent (scalper wants a spike, others just need "not dead")
    vol_ratio = entry_ind.get("vol_ratio")
    if vol_ratio is not None and vol_ratio < rules["min_vol_ratio"]:
        decision.reason = (
            f"volume filter: vol_ratio={vol_ratio} on {entry_tf} below persona minimum "
            f"{rules['min_vol_ratio']}"
        )
        return decision

    # 7) minimum RR filter -- reject geometrically bad trades regardless of confidence
    if not check_min_rr(stop_loss_pct, take_profit_pct, rules["min_rr"]):
        rr = round(take_profit_pct / stop_loss_pct, 2) if stop_loss_pct else 0
        decision.reason = f"RR filter: rr={rr} below persona minimum {rules['min_rr']}"
        return decision

    # 8) blend LLM confidence with rule-based agreement strength, then gate
    #    on the higher, persona-aware threshold (was a flat 0.45 for everyone).
    agreement_ratio = agree / total if total else 0
    rule_confidence = 0.5 + 0.5 * agreement_ratio  # 0.5..1.0
    blended = round(0.6 * llm_confidence + 0.4 * rule_confidence, 3)
    decision.blended_confidence = blended

    if blended < rules["min_confidence"]:
        decision.reason = (
            f"confidence filter: blended_confidence={blended} "
            f"(llm={llm_confidence}, rule={round(rule_confidence,3)}) "
            f"below persona minimum {rules['min_confidence']}"
        )
        return decision

    decision.approved = True
    decision.reason = (
        f"approved: regime={regime}, consensus={consensus} ({agree}/{total} tfs agree), "
        f"blended_confidence={blended}"
    )
    return decision
