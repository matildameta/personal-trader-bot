import logging
import os
import sys
import time
import pandas as pd
from hyperliquid.utils import constants

from .config import Config
from .db import SharedDB
from .ai_pipeline import AIPipeline, STRATEGIES
from .indicators import compute_indicators, format_multi_timeframe_summary
from .fundamentals import format_fundamental_summary
from .risk_engine import (
    plan_position, KillSwitch, atr_floor_stop,
    drawdown_throttle, volatility_adjusted_leverage,
)
from .rule_engine import evaluate_trade
from .hyperliquid_client import HyperliquidClient
from .telegram_reporter import TelegramReporter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("main")


def drop_unclosed_candle(raw_candles):
    """Hyperliquid's candles_snapshot(start, end=now) includes the
    still-forming candle as the last element. Trading off it means your
    'entry_price' and indicators are computed from an incomplete bar that
    can still move a lot before it actually closes -- classic source of
    signals that look great at decision time and are stale a minute later.
    Drop it: only trade on confirmed, closed candles."""
    if not raw_candles:
        return raw_candles
    now_ms = int(time.time() * 1000)
    last = raw_candles[-1]
    close_time = last.get("T") or last.get("t")
    if close_time and close_time > now_ms:
        return raw_candles[:-1]
    return raw_candles


def candles_to_df(raw_candles) -> pd.DataFrame:
    rows = [
        {
            "open_time": c.get("t"),
            "open": float(c["o"]), "high": float(c["h"]),
            "low": float(c["l"]), "close": float(c["c"]),
            "volume": float(c["v"]),
        }
        for c in raw_candles
    ]
    return pd.DataFrame(rows)


def drain_pending_commands(db: SharedDB, hl: HyperliquidClient, tg: TelegramReporter, lang: str):
    """Actions that require real exchange access (i.e. not a plain setting)
    are queued by control_bot in pending_commands and executed here, since
    only core_engine holds the exchange keys."""
    for cmd in db.get_pending_commands():
        try:
            if cmd["type"] == "closeall":
                closed = hl.close_all_positions()
                for symbol in closed:
                    open_trades = db.open_trades_for_symbol(symbol)
                    for tr in open_trades:
                        # compute REAL pnl at current mark price (not 0)
                        try:
                            pnl_info = hl.get_position_pnl(
                                symbol, side=tr["side"],
                                entry_price=float(tr["entry_price"]),
                                size=float(tr.get("size", 1.0)),
                            )
                            ca_exit = pnl_info.get("exit_price") or 0
                            ca_pnl = pnl_info.get("pnl_usd") or 0.0
                        except Exception:
                            ca_exit = 0
                            ca_pnl = 0.0
                        db.close_trade(tr["id"], exit_price=ca_exit, pnl_usd=ca_pnl, closed_by="manual_closeall")
                db.log("INFO", f"closeall executed: {closed}")
                tg.closeall_done(lang, closed)
            elif cmd["type"] == "restart_engine":
                db.mark_command(cmd["id"], "done")
                db.log("INFO", "restart_engine requested via Telegram; restarting process now")
                tg._send("🔄 موتور معاملاتی در حال ری‌استارت...\nTrading engine restarting...")
                os.execv(sys.executable, [sys.executable] + sys.argv)
            elif cmd["type"].startswith("transfer:"):
                # format: "transfer:<direction>:<amount>"  direction = toperp|tospot
                _, direction, amount_s = cmd["type"].split(":")
                amount = float(amount_s)
                to_perp = (direction == "toperp")
                hl.exchange.usd_class_transfer(round(amount, 2), to_perp)
                where = "Perp" if to_perp else "Spot"
                db.log("INFO", f"transfer {amount} -> {where} executed")
                tg._send(
                    f"✅ انتقال انجام شد: {amount}$ → {where}\n"
                    f"Transfer done: ${amount} → {where}"
                )
            db.mark_command(cmd["id"], "done")
        except Exception as e:
            logger.exception(f"Failed to execute command {cmd}")
            db.log("ERROR", f"command {cmd['type']} failed: {e}")
            db.mark_command(cmd["id"], "failed")


def sync_live_state(db: SharedDB, hl: HyperliquidClient):
    """Written every cycle so control_bot can show /status, /positions,
    /balance without ever holding exchange keys itself.

    NOTE: spot_usd + perp_usd are reported SEPARATELY for display, but the
    single authoritative `balance_usd` total is taken from get_usdc_balance()
    which already sums both exactly once (no double-counting when margin is
    moved spot->perp for open positions).
    """
    try:
        spot = hl.get_spot_usdc()
        perp = hl.get_perp_usdc()
        total = hl.get_usdc_balance()  # authoritative total, no double count
        positions = hl.get_open_positions()
        db.set_live_state("balance_usd", round(total, 2))
        db.set_live_state("spot_usd", round(spot, 2))
        db.set_live_state("perp_usd", round(perp, 2))
        db.set_live_state("open_positions", positions)
    except Exception as e:
        logger.warning(f"Failed to sync live state: {e}")


def run_cycle(cfg: Config, db: SharedDB, hl: HyperliquidClient, tg: TelegramReporter):
    settings = db.get_settings()
    lang = settings["language"]

    # rebuild the AI pipeline from current settings every cycle so that
    # /strategy (set from Telegram) takes effect on the very next cycle,
    # no restart needed. We also reload config.yaml from disk each cycle so
    # AI provider keys set via the control bot are picked up live -- the
    # engine no longer needs a restart when a key changes; the change simply
    # applies on the next analysis cycle.
    cfg.reload()
    llm = AIPipeline.from_settings(cfg.llm, settings, reporter=tg)

    drain_pending_commands(db, hl, tg, lang)
    sync_live_state(db, hl)

    # --- per-cycle safety + health alerts (each honors its own on/off toggle) ---
    try:
        _check_alerts(db, hl, tg, lang)
    except Exception as e:
        logger.warning(f"alert check failed: {e}")
    try:
        _check_server_health(db, tg, lang)
    except Exception as e:
        logger.warning(f"server health check failed: {e}")
    try:
        _detect_deposits_withdrawals(db, hl, tg, lang)
    except Exception as e:
        logger.warning(f"deposit/withdraw detect failed: {e}")
    try:
        _check_extra_alerts(db, hl, tg, lang, cfg)
    except Exception as e:
        logger.warning(f"extra alerts failed: {e}")

    # --- verify the active strategy is actually in effect (set live from the
    # control bot). If it changed since the last cycle, confirm to the owner
    # via the reporter so a silent persona mismatch can never go unnoticed.
    current_strategy = settings.get("strategy", "balanced")
    prev_strategy = db.get_live_state().get("active_strategy")
    if prev_strategy is None:
        # first cycle after start: record without re-announcing
        db.set_live_state("active_strategy", current_strategy)
    elif prev_strategy != current_strategy:
        db.set_live_state("active_strategy", current_strategy)
        try:
            strat_info = STRATEGIES.get(current_strategy, {})
            label = strat_info.get(f"label_{lang}", current_strategy)
            tg.strategy_activated(
                lang,
                label=label,
                key=current_strategy,
            )
            db.log("INFO", f"Strategy switched {prev_strategy} -> {current_strategy}; confirmed via reporter")
        except Exception as e:
            logger.warning(f"Could not announce strategy switch: {e}")

    if settings["paused"]:
        logger.info("Bot is paused (set via control_bot). Skipping cycle.")
        return

    # Balances. On Hyperliquid, opening a perp position auto-draws margin
    # from spot when perp is empty (verified live), so tradable capital is
    # the authoritative total held by the wallet — get_usdc_balance() sums
    # spot free + perp margin exactly once (no double count when margin
    # moved spot->perp). capital_usd (via /setcapital) is a ceiling.
    perp_balance = hl.get_perp_usdc()
    spot_balance = hl.get_spot_usdc()
    live_balance = hl.get_usdc_balance()  # authoritative total, no double count
    cap = float(settings["capital_usd"])
    tradable = live_balance  # perp + spot both usable as margin
    effective_capital = min(tradable, cap) if cap > 0 else tradable
    db.set_live_state("balance_usd", live_balance)
    db.set_live_state("spot_usd", round(spot_balance, 2))
    db.set_live_state("perp_usd", round(perp_balance, 2))
    capital_usd = effective_capital
    max_leverage = int(settings["max_leverage"])
    risk_pct = float(settings["risk_per_trade_pct"])
    min_notional = float(settings["min_notional_usd"])

    # --- kill switch: closed PnL today + unrealized PnL of anything still open ---
    today_start = time.time() - (time.time() % 86400)
    todays_trades = db.trades_since(today_start)
    closed_pnl_today = sum(t_["pnl_usd"] or 0 for t_ in todays_trades if t_["status"] == "closed")
    open_positions = hl.get_open_positions()
    unrealized_pnl = sum(p["unrealized_pnl"] for p in open_positions)
    pnl_today = closed_pnl_today + unrealized_pnl

    consecutive_losses = 0
    for t_ in sorted(todays_trades, key=lambda x: -x["ts"]):
        if t_["status"] != "closed":
            continue
        if (t_["pnl_usd"] or 0) < 0:
            consecutive_losses += 1
        else:
            break

    ks = KillSwitch(
        max_daily_loss_pct=float(settings["max_daily_loss_pct"]),
        max_consecutive_losses=int(settings["max_consecutive_losses"]),
    )
    trip_reason = ks.check(capital_usd=capital_usd, pnl_today_usd=pnl_today, consecutive_losses=consecutive_losses)
    if trip_reason:
        db.set_setting("paused", True)
        db.log("WARNING", f"Kill switch tripped: {trip_reason}")
        tg.kill_switch(lang, trip_reason, round(pnl_today, 2))
        return

    positions_by_symbol = {p["symbol"]: p for p in open_positions}

    # --- detect positions that closed on-exchange (SL/TP hit) and report PnL ---
    open_db_trades = db.open_trades()
    for tr in open_db_trades:
        sym = tr["symbol"]
        if sym in positions_by_symbol:
            continue  # still open on exchange
        # position is gone from exchange but DB says open -> it closed
        try:
            pnl = hl.get_position_pnl(sym, side=tr["side"], entry_price=tr["entry_price"], size=tr.get("size", 1.0))
            exit_px = pnl.get("exit_price")
            pnl_usd = pnl.get("pnl_usd")
            # held time computed from when we opened it (DB ts) to now
            held = "?"
            if tr.get("ts"):
                secs = int(time.time() - float(tr["ts"]))
                h, rem = divmod(secs, 3600)
                m, s = divmod(rem, 60)
                held = (f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s"))
            db.close_trade(tr["id"], exit_price=exit_px, pnl_usd=pnl_usd, closed_by="sl/tp")
            db.log("INFO", f"{sym} closed on-exchange: pnl={pnl_usd}")
            # full detailed close report (toggle: trade_closed)
            tg.trade_closed(
                lang, network=cfg.network, symbol=sym, side=tr["side"],
                entry_price=tr["entry_price"], exit_price=exit_px,
                pnl=f"${pnl_usd:.2f}" if pnl_usd is not None else "?",
                pnl_pct="?", closed_by="SL/TP", held_time=held,
            )
            # short P&L ping (toggle: trade_pnl_short) — no explanation
            if pnl_usd is not None:
                tg.trade_pnl_short(lang, sym, tr["side"], pnl_usd)
                # post-mortem with AI reasoning (toggle: trade_postmortem)
                entry_reason = (tr.get("reasoning") or "no data")[:200]
                exit_reason = "TP/SL hit on-exchange" if abs(pnl_usd) > 0 else "manual"
                tg.trade_postmortem(lang, sym, tr["side"], tr["entry_price"],
                                     exit_px, pnl_usd, entry_reason, exit_reason)
                # loss streak (toggle: loss_streak_warn)
                if pnl_usd < 0:
                    streak = db.get_live_state().get("loss_streak", 0)
                    streak = int(streak) + 1
                    db.set_live_state("loss_streak", streak)
                    if streak >= 5:
                        tg.loss_streak_warn(lang, streak, pnl_usd)
                else:
                    db.set_live_state("loss_streak", 0)
        except Exception as e:
            logger.warning(f"Could not resolve closed trade {tr['id']}: {e}")

    symbols = settings.get("symbols") or cfg.symbols
    timeframes = settings.get("timeframes") or cfg.timeframes
    persona = current_strategy.split("__")[-1] if "__" in current_strategy else current_strategy

    # new tunables -- all have safe defaults so this works even before
    # you add them to the settings UI/DB
    cooldown_seconds = int(settings.get("cooldown_seconds", 900))
    max_trades_per_symbol_per_day = int(settings.get("max_trades_per_symbol_per_day", 3))
    reversal_confirm_cycles = int(settings.get("reversal_confirm_cycles", 2))
    atr_min_multiple = float(settings.get("atr_sl_min_multiple", 1.2))

    for symbol in symbols:
        try:
            per_tf_indicators = {}
            for tf in timeframes:
                raw = hl.get_candles(symbol, tf, lookback_count=200)
                raw = drop_unclosed_candle(raw)          # <-- only closed candles
                df = candles_to_df(raw)
                if len(df) < 50:
                    logger.warning(f"Not enough candles for {symbol} {tf}, skipping")
                    continue
                per_tf_indicators[tf] = compute_indicators(df)

            if not per_tf_indicators:
                continue

            summary = format_multi_timeframe_summary(symbol, per_tf_indicators)
            market_ctx = hl.get_market_context(symbol)
            fundamental_summary = format_fundamental_summary(symbol, market_ctx)
            signal = llm.analyze(symbol, summary, fundamental_summary)
            db.log("INFO", f"{symbol} signal: {signal}")

            if getattr(llm, "hard_rate_limited", False):
                logger.error("AI hard rate-limited; pausing bot and halting trading.")
                db.set_setting("paused", True)
                tg.error(lang, symbol,
                         "AI RATE-LIMITED: both OpenRouter keys exhausted. "
                         "Bot paused — add a fresh key and restart the engine.")
                return

            # cheap pre-filter: still bail immediately on outright "hold"
            if signal["signal"] == "hold":
                logger.info(f"{symbol}: LLM said hold, skipping")
                try:
                    tg.hold_signal(lang, symbol, confidence=round(float(signal.get("confidence", 0)), 2),
                                    reasoning=(signal.get("reasoning") or "hold")[:400])
                except Exception as e:
                    logger.warning(f"hold_signal report failed: {e}")
                continue

            side = signal["signal"]
            entry_tf = next((tf for tf in timeframes if tf in per_tf_indicators), None)
            htf = next((tf for tf in reversed(timeframes) if tf in per_tf_indicators), entry_tf)
            entry_price = per_tf_indicators[entry_tf]["close"]
            entry_atr_pct = per_tf_indicators[entry_tf].get("atr_pct")

            # --- ATR floor on the LLM's stop before it ever reaches the rule engine ---
            sl_pct, tp_pct = atr_floor_stop(
                side=side,
                stop_loss_pct=float(signal["suggested_stop_loss_pct"]),
                take_profit_pct=float(signal["suggested_take_profit_pct"]),
                atr_pct=entry_atr_pct,
                min_atr_multiple=atr_min_multiple,
            )

            # --- the deterministic gate: this is what was missing ---
            decision = evaluate_trade(
                persona=persona,
                side=side,
                llm_confidence=float(signal.get("confidence", 0)),
                per_tf_indicators=per_tf_indicators,
                entry_tf=entry_tf,
                htf=htf,
                stop_loss_pct=sl_pct,
                take_profit_pct=tp_pct,
            )
            db.log("INFO", f"{symbol} rule_engine: {decision.reason}")

            if not decision.approved:
                logger.info(f"{symbol}: rejected by rule engine — {decision.reason}")
                try:
                    tg.hold_signal(lang, symbol, confidence=round(decision.blended_confidence, 2),
                                    reasoning=f"[rule engine] {decision.reason}"[:400])
                except Exception as e:
                    logger.warning(f"hold_signal report failed: {e}")
                continue

            existing = positions_by_symbol.get(symbol)

            if existing:
                if existing["side"] == side:
                    logger.info(f"{symbol}: already {side}, skip (no stacking)")
                    db.set_live_state(f"pending_reversal_{symbol}", None)
                    continue

                # --- reversal now needs N consecutive opposite signals, not one ---
                key = f"pending_reversal_{symbol}"
                pending = db.get_live_state().get(key) or {}
                if pending.get("side") == side:
                    count = int(pending.get("count", 0)) + 1
                else:
                    count = 1
                db.set_live_state(key, {"side": side, "count": count})

                if count < reversal_confirm_cycles:
                    logger.info(
                        f"{symbol}: opposite signal {side} seen {count}/{reversal_confirm_cycles} "
                        f"cycles, not reversing yet"
                    )
                    continue

                hl.close_position(symbol)
                open_trades = db.open_trades_for_symbol(symbol)
                for tr in open_trades:
                    # compute REAL pnl at current mark price (not 0)
                    try:
                        pnl_info = hl.get_position_pnl(
                            symbol, side=tr["side"],
                            entry_price=float(tr["entry_price"]),
                            size=float(tr.get("size", 1.0)),
                        )
                        rev_exit = pnl_info.get("exit_price") or 0
                        rev_pnl = pnl_info.get("pnl_usd") or 0.0
                    except Exception:
                        rev_exit = 0
                        rev_pnl = 0.0
                    db.close_trade(tr["id"], exit_price=rev_exit, pnl_usd=rev_pnl, closed_by="reversal")
                db.log("INFO", f"{symbol}: closed {existing['side']} for reversal into {side} "
                                f"(confirmed over {count} cycles)")
                db.set_live_state(key, None)
                db.set_live_state(f"cooldown_until_{symbol}", time.time() + cooldown_seconds)

            # --- cooldown after any recent close on this symbol ---
            cooldown_until = float(db.get_live_state().get(f"cooldown_until_{symbol}", 0) or 0)
            if time.time() < cooldown_until:
                logger.info(f"{symbol}: in cooldown for {int(cooldown_until - time.time())}s more, skipping")
                continue

            # --- max trades per symbol per day ---
            today_symbol_trades = [t_ for t_ in todays_trades if t_["symbol"] == symbol]
            if len(today_symbol_trades) >= max_trades_per_symbol_per_day:
                logger.info(f"{symbol}: hit max_trades_per_symbol_per_day "
                            f"({max_trades_per_symbol_per_day}), skipping")
                continue

            # --- position sizing mode (unchanged) ---
            sizing_mode = settings.get("sizing_mode", "auto")
            ceiling_pct = float(settings.get("trade_capital_pct", 50))
            if sizing_mode == "fixed":
                use_pct = ceiling_pct
            else:
                use_pct = min(float(signal.get("suggested_capital_pct", 50)), ceiling_pct)
            use_pct = max(1.0, min(100.0, use_pct))
            trade_capital = capital_usd * (use_pct / 100.0)

            # --- drawdown-aware risk + volatility-aware leverage ---
            effective_risk_pct = drawdown_throttle(
                base_risk_pct=risk_pct, pnl_today_usd=pnl_today,
                capital_usd=capital_usd, consecutive_losses=consecutive_losses,
            )
            effective_max_leverage = volatility_adjusted_leverage(
                max_leverage=max_leverage, atr_pct=entry_atr_pct,
            )
            logger.info(
                f"{symbol}: sizing_mode={sizing_mode} use_pct={use_pct:.1f}% "
                f"trade_capital={trade_capital:.2f} risk_pct={effective_risk_pct} "
                f"(base {risk_pct}) leverage_cap={effective_max_leverage} (base {max_leverage}) "
                f"sl_pct={sl_pct} tp_pct={tp_pct} (llm suggested "
                f"{signal['suggested_stop_loss_pct']}/{signal['suggested_take_profit_pct']})"
            )

            plan = plan_position(
                capital_usd=trade_capital,
                entry_price=entry_price,
                side=side,
                stop_loss_pct=sl_pct,
                take_profit_pct=tp_pct,
                max_leverage=effective_max_leverage,
                risk_per_trade_pct=effective_risk_pct,
                min_notional_usd=min_notional,
            )

            if plan.rejected:
                logger.warning(f"{symbol}: position rejected — {plan.rejection_reason}")
                db.log("WARNING", f"{symbol} rejected: {plan.rejection_reason}")
                continue

            hl.set_leverage(symbol, int(plan.leverage) or 1)
            result = hl.place_market_order_with_sl_tp(
                symbol=symbol,
                is_buy=(side == "long"),
                size=plan.size_usd,
                stop_loss_price=plan.stop_loss_price,
                take_profit_price=plan.take_profit_price,
            )
            logger.info(f"Order result for {symbol}: {result}")

            db.open_trade(
                symbol=symbol, side=side, entry_price=entry_price,
                size=plan.size_usd, leverage=plan.leverage,
                effective_risk_pct=plan.effective_risk_pct,
                stop_loss=plan.stop_loss_price, take_profit=plan.take_profit_price,
                llm_model=signal["_model_used"], llm_confidence=signal["confidence"],
                reasoning=f"{signal['reasoning']} | rule_engine: {decision.reason}",
            )

            margin_usd = round(plan.notional_usd / plan.leverage, 2) if plan.leverage else plan.notional_usd
            tg.trade_opened(
                lang, network=cfg.network, symbol=symbol, side=side, entry=entry_price,
                size=plan.size_usd, notional=plan.notional_usd, leverage=plan.leverage,
                margin_usd=margin_usd, capital_pct=round(use_pct, 1),
                sl=plan.stop_loss_price, sl_pct=sl_pct,
                tp=plan.take_profit_price, tp_pct=tp_pct,
                risk_pct=plan.effective_risk_pct, model=signal["_model_used"],
                confidence=decision.blended_confidence, reasoning=signal["reasoning"],
            )
            db.set_live_state("last_trade_open_ts", int(time.time()))

        except Exception as e:
            logger.exception(f"Error processing {symbol}")
            db.log("ERROR", f"{symbol}: {e}")
            tg.error(lang, symbol, str(e))

    # accumulate AI call count for the cost report
    try:
        prev_calls = int(db.get_live_state().get("ai_calls", 0) or 0)
        db.set_live_state("ai_calls", prev_calls + getattr(llm, "call_count", 0))
    except Exception:
        pass


def _summary_kwargs(db: SharedDB, hl: HyperliquidClient, lang: str, since_ms: int) -> dict:
    """Shared summary payload (counts, win rate, pnl, positions, balance)."""
    settings = db.get_settings()
    trades = db.trades_since(since_ms / 1000)
    closed = [t_ for t_ in trades if t_["status"] == "closed"]
    wins = [t_ for t_ in closed if (t_["pnl_usd"] or 0) > 0]
    win_rate = round(100 * len(wins) / len(closed), 1) if closed else 0.0
    pnl = round(sum(t_["pnl_usd"] or 0 for t_ in closed), 2)
    open_positions = hl.get_open_positions()
    balance = hl.get_usdc_balance()
    strat = settings.get("strategy", "balanced")
    strat_label = STRATEGIES.get(strat, {}).get(f"label_{lang}", strat)
    return dict(
        count=len(trades), win_rate=win_rate, pnl=f"{pnl:+.2f}",
        open_positions=len(open_positions), balance=f"{balance:.2f}",
        strategy=strat_label,
    )


def _send_periodic_reports(cfg, db, hl, tg, lang):
    """Send week/month summaries + analytical reports per their toggles."""
    settings = db.get_settings()
    now = time.time()
    # weekly (last 7d)
    if settings.get("report_weekly_summary", True):
        since = int(now - 7 * 86400)
        tg.weekly_summary(lang, **_summary_kwargs(db, hl, lang, since * 1000))
    # monthly (last 30d)
    if settings.get("report_monthly_summary", True):
        since = int(now - 30 * 86400)
        tg.monthly_summary(lang, **_summary_kwargs(db, hl, lang, since * 1000))
    # analytical: best/worst + fee/funding
    try:
        addr = cfg.hyperliquid["account_address"]
        net = cfg.network
        # reuse hl_pnl compute
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from hl_pnl import get_fills, compute_pnl_summary
        fills = get_fills(addr, net, int((now - 7 * 86400) * 1000))
        s = compute_pnl_summary(fills)
        if s["count"] > 0:
            tg.best_worst(lang, s["best_symbol"], s["best_pnl"], s["worst_symbol"],
                          s["worst_pnl"], s["count"], s["win_rate"])
    except Exception as e:
        logger.warning(f"best/worst report failed: {e}")
    # fee + funding
    try:
        total_fee = 0.0
        total_funding = 0.0
        addr = cfg.hyperliquid_account_address
        net = cfg.network
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
        api = constants.TESTNET_API_URL if net == "testnet" else constants.MAINNET_API_URL
        info = Info(api, skip_ws=True)
        fills = info.user_fills(addr)
        for f in fills:
            total_fee += float(f.get("fee") or 0)
            pnl = float(f.get("closedPnl") or 0)
        fh = info.user_funding_history(addr, int((now - 7 * 86400) * 1000))
        for rec in fh:
            total_funding += float(rec.get("delta", {}).get("usdc") or 0)
        tg.fee_funding_report(lang, round(total_fee, 4), round(total_funding, 4),
                               round(total_fee + total_funding, 4), "۷ روز" if lang == "fa" else "7d")
    except Exception as e:
        logger.warning(f"fee/funding report failed: {e}")


def send_daily_summary(cfg: Config, db: SharedDB, hl: HyperliquidClient, tg: TelegramReporter, lang: str):
    """Build a rich daily report from DB + live state and push it."""
    settings = db.get_settings()
    interval_h = float(settings.get("report_interval_hours", 24) or 0)
    if interval_h <= 0:
        return  # reporting disabled
    now = time.time()
    day_start = now - (now % 86400)
    kw = _summary_kwargs(db, hl, lang, day_start * 1000)
    tg.daily_summary(lang, **kw)
    # also push the weekly/monthly/analytical reports (each honors its toggle)
    try:
        _send_periodic_reports(cfg, db, hl, tg, lang)
    except Exception as e:
        logger.warning(f"periodic analytical reports failed: {e}")


def _check_extra_alerts(db: SharedDB, hl: HyperliquidClient, tg: TelegramReporter, lang: str, cfg):
    """Newer alerts: inactivity, AI cost, recovery, exchange health, whales."""
    now = time.time()
    try:
        # 1. inactivity: no position opened for 8h AND bot had no activity
        last_open = float(db.get_live_state().get("last_trade_open_ts", 0) or 0)
        if last_open == 0:
            # seed on first run with now so it doesn't fire immediately
            db.set_live_state("last_trade_open_ts", int(now))
        elif (now - last_open) >= 8 * 3600:
            tg.inactivity_warn(lang, 8)
            # reset so it doesn't spam every cycle; re-arm after another 8h
            db.set_live_state("last_trade_open_ts", int(now))

        # 2. AI cost report every 12h
        last_cost = float(db.get_live_state().get("last_ai_cost_ts", 0) or 0)
        if (now - last_cost) >= 12 * 3600:
            calls = int(db.get_live_state().get("ai_calls", 0) or 0)
            tokens = int(db.get_live_state().get("ai_tokens", 0) or 0)
            tg.ai_cost_report(lang, calls, tokens, "۱۲ ساعت" if lang == "fa" else "12h")
            db.set_live_state("last_ai_cost_ts", int(now))
            db.set_live_state("ai_calls", 0)
            db.set_live_state("ai_tokens", 0)

        # 3. recovery: equity back to peak after a drawdown
        balance = hl.get_usdc_balance()
        peak = float(db.get_live_state().get("peak_equity") or balance)
        in_dd = bool(db.get_live_state().get("in_drawdown", False))
        if balance >= peak and in_dd and peak > 0:
            tg.recovery_alert(lang, round(peak, 2), round(balance, 2))
            db.set_live_state("in_drawdown", False)
        if balance > peak:
            db.set_live_state("peak_equity", round(balance, 2))

        # 4. exchange health: ping the API, alert on failure/slow
        t0 = time.time()
        try:
            hl.get_usdc_balance()
            rtt = time.time() - t0
            if rtt > 5.0:
                tg.exchange_health(lang, "⚠️ کند / Slow", f"RTT {rtt:.1f}s")
        except Exception as e:
            tg.exchange_health(lang, "❌ قطع / Down", str(e)[:200])

        # 5. whale alert: large open position on our symbols (OI spike heuristic)
        try:
            symbols = db.get_settings().get("symbols") or cfg.symbols
            meta, ctxs = hl.info.meta_and_asset_ctxs()
            names = [a["name"] for a in meta["universe"]]
            for sym in symbols:
                if sym in names:
                    c = ctxs[names.index(sym)]
                    oi = float(c.get("openInterest", 0) or 0)
                    mark = float(c.get("markPx", 0) or 0)
                    notional = oi * mark
                    # alert if single-position notional > $1M (whale threshold)
                    if notional > 1_000_000:
                        last_w = float(db.get_live_state().get(f"whale_{sym}", 0) or 0)
                        if (now - last_w) >= 3600:  # max once per hour per symbol
                            tg.whale_alert(lang, sym, "OPEN", round(notional, 0), round(mark, 2))
                            db.set_live_state(f"whale_{sym}", int(now))
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"extra alerts failed: {e}")
# Instant alerts (liquidation / drawdown / TP-SL approach / funding) and
# server-health monitoring. Each reads its own on/off toggle via the reporter.
# ---------------------------------------------------------------------------

def _check_alerts(db: SharedDB, hl: HyperliquidClient, tg: TelegramReporter, lang: str):
    """Run lightweight, per-cycle health/safety checks on open positions."""
    try:
        positions = hl.get_open_positions()
    except Exception:
        return
    for p in positions:
        sym = p["symbol"]
        liq = p.get("liq")
        entry = p.get("entry_price")
        cur = None
        try:
            ctx = hl.get_market_context(sym)
            cur = ctx.get("mark_price")
        except Exception:
            cur = None
        if liq is None or cur is None or entry is None or entry == 0:
            continue
        # distance (in %) from current price to liquidation price
        dist = abs((cur - liq) / entry) * 100
        if dist <= 15.0:  # within 15% of liquidation -> warn (essential)
            tg.liquidation_warn(lang, sym, p["side"], entry, liq, cur, round(dist, 2))

        # TP/SL approach (within 1% of either) -> optional alert
        if p.get("tp") or p.get("sl"):
            for kind, lvl in (("TP", p.get("tp")), ("SL", p.get("sl"))):
                if not lvl:
                    continue
                dd = abs((cur - lvl) / cur) * 100 if cur else None
                if dd is not None and dd <= 1.0:
                    tg.tp_sl_near(lang, sym, kind, lvl, round(dd, 2))

        # high funding alert (per-8h rate above threshold)
        try:
            ctx = hl.get_market_context(sym)
            fr = ctx.get("funding_rate")
            if fr is not None and abs(fr) >= 0.001:  # >= 0.1% per 8h
                notional = p["size"] * (cur or entry)
                tg.funding_high(lang, sym, round(fr * 100, 4), round(notional, 2),
                                round(abs(fr) * notional, 4))
        except Exception:
            pass

    # drawdown from peak equity (tracked in live_state)
    try:
        balance = hl.get_usdc_balance()
        peak = float(db.get_live_state().get("peak_equity") or balance)
        if balance > peak:
            db.set_live_state("peak_equity", round(balance, 2))
            peak = balance
        if peak > 0:
            drop = (peak - balance) / peak * 100
            if drop >= 20.0:  # >= 20% drawdown -> alert
                tg.drawdown_warn(lang, round(drop, 1), round(balance, 2), round(peak, 2))
                db.set_live_state("in_drawdown", True)
    except Exception:
        pass


def _check_server_health(db: SharedDB, tg: TelegramReporter, lang: str):
    """Warn if CPU/RAM/disk crosses safe thresholds."""
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory().percent
        disk = psutil.disk_usage("/").percent
    except Exception as e:
        logger.warning(f"server health check failed: {e}")
        return
    note = ""
    if cpu >= 85:
        note += "🔥 CPU بسیار بالا / CPU very high\n"
    if ram >= 85:
        note += "🧠 RAM تقریباً پر / RAM nearly full\n"
    if disk >= 85:
        note += "💽 دیسک تقریباً پر / Disk nearly full\n"
    if cpu >= 85 or ram >= 85 or disk >= 85:
        # throttle: only re-alert every 30 min
        last = float(db.get_live_state().get("last_health_alert", 0) or 0)
        if (time.time() - last) >= 1800:
            tg.server_health(lang, round(cpu, 1), round(ram, 1), round(disk, 1), note)
            db.set_live_state("last_health_alert", int(time.time()))


def _detect_deposits_withdrawals(db: SharedDB, hl: HyperliquidClient, tg: TelegramReporter, lang: str):
    """Detect real on-chain deposits/withdrawals from the Hyperliquid ledger
    (user_non_funding_ledger_updates). Unlike a naive balance delta, this only
    fires on actual bridge/deposit/withdraw/transfer events, not PnL or funding.
    State is tracked by the latest ledger timestamp we've already reported."""
    try:
        addr = hl.account_address
        api = constants.TESTNET_API_URL if hl.network == "testnet" else constants.MAINNET_API_URL
        from hyperliquid.info import Info
        info = Info(api, skip_ws=True)
        since_ms = int((time.time() - 7 * 86400) * 1000)
        ledger = info.user_non_funding_ledger_updates(addr, since_ms)
        if not ledger:
            return
        last_ts = float(db.get_live_state().get("last_ledger_ts", 0) or 0)
        for rec in ledger:
            t = int(rec.get("time", 0))
            if t <= last_ts:
                continue  # already reported
            d = rec.get("delta", {}) or {}
            typ = d.get("type", "")
            amount = float(d.get("usdcValue") or d.get("amount") or 0)
            if amount <= 0:
                continue
            # classify: money arriving to us = deposit, leaving = withdrawal
            if typ in ("deposit", "receive", "bridge"):
                tg.deposit(lang, round(amount, 2), round(hl.get_usdc_balance(), 2))
            elif typ in ("withdraw", "send"):
                tg.withdrawal(lang, round(amount, 2), round(hl.get_usdc_balance(), 2))
            elif typ == "spotTransfer":
                # could be in or out depending on direction vs our address
                if d.get("destination", "").lower() == addr.lower():
                    tg.deposit(lang, round(amount, 2), round(hl.get_usdc_balance(), 2))
                elif d.get("user", "").lower() == addr.lower():
                    tg.withdrawal(lang, round(amount, 2), round(hl.get_usdc_balance(), 2))
        # advance watermark
        new_max = max((int(r.get("time", 0)) for r in ledger), default=0)
        if new_max > last_ts:
            db.set_live_state("last_ledger_ts", new_max)
    except Exception as e:
        logger.warning(f"deposit/withdraw detect failed: {e}")


def main():
    cfg = Config("config.yaml")
    # seed report on/off defaults alongside other defaults so the toggles
    # exist in the settings table from first boot
    from .report_config import default_settings
    seed = dict(cfg.defaults or {})
    seed.update(default_settings())
    db = SharedDB(cfg.shared_db_path, seed_defaults=seed)
    hl = HyperliquidClient(account_address=cfg.hyperliquid_account_address, secret_key=cfg.hyperliquid_secret_key, network=cfg.network)
    # wire a settings provider so every reporter method can honor the
    # on/off toggles set from the control bot panel
    tg = TelegramReporter(**cfg.telegram, settings_provider=db.get_settings)

    settings = db.get_settings()
    tg.started(
        settings["language"], cfg.network, settings.get("symbols", cfg.symbols),
        "4-stage AI pipeline (chart→fundamental→synthesis→decision, fixed)",
        settings["max_leverage"], settings["risk_per_trade_pct"], settings["capital_usd"],
    )
    db.log("INFO", f"Engine started on {cfg.network}")

    last_report = time.time()
    while True:
        try:
            run_cycle(cfg, db, hl, tg)
        except Exception as e:
            logger.exception("Fatal error in cycle")
            db.log("ERROR", f"Fatal cycle error: {e}")

        # periodic rich daily summary, independent of trading cycles
        try:
            interval_h = float(db.get_settings().get("report_interval_hours", 24) or 0)
            if interval_h > 0 and (time.time() - last_report) >= interval_h * 3600:
                send_daily_summary(cfg, db, hl, tg, db.get_settings().get("language", "en"))
                last_report = time.time()
        except Exception as e:
            logger.warning(f"Daily summary failed: {e}")

        time.sleep(int(db.get_settings().get("loop_interval_seconds", cfg.loop_interval_seconds) or cfg.loop_interval_seconds))


if __name__ == "__main__":
    main()
