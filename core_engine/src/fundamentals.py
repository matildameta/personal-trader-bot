"""
Formats the "fundamental" market snapshot (funding rate, open interest,
24h volume/change) fetched from HyperliquidClient.get_market_context()
into a compact text block for the AI pipeline's fundamental-analysis
stage (see ai_pipeline.py). Kept separate from indicators.py because it
describes market *positioning/sentiment*, not price-action technicals.
"""


def format_fundamental_summary(symbol: str, ctx: dict) -> str:
    if not ctx:
        return (
            f"[{symbol}] No fundamental/on-chain data available this cycle "
            f"(market context fetch failed) -- reason using technical data only."
        )

    funding = ctx.get("funding_rate")
    funding_pct = round(funding * 100, 4) if funding is not None else None
    funding_bias = None
    if funding_pct is not None:
        if funding_pct > 0.01:
            funding_bias = "longs paying shorts (crowded long positioning)"
        elif funding_pct < -0.01:
            funding_bias = "shorts paying longs (crowded short positioning)"
        else:
            funding_bias = "roughly neutral positioning"

    lines = [
        f"[{symbol}] mark_price={ctx.get('mark_price')} "
        f"24h_change={ctx.get('day_change_pct')}% "
        f"24h_notional_volume_usd={ctx.get('day_notional_volume_usd')}",
        f"funding_rate={funding_pct}% ({funding_bias}) "
        f"open_interest={ctx.get('open_interest')} "
        f"premium={ctx.get('premium')}",
    ]
    return "\n".join(lines)
