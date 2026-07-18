"""
One-way push notifications to Telegram (trade opened/closed, deposits,
withdrawals, daily/weekly/monthly summaries, kill-switch trips, errors,
hold-skips, strategy changes, instant alerts, server-health warnings).

Uses raw HTTP against the Bot API -- this process only sends messages, it
never needs to receive commands (that's control_bot's job, on its own token).

Every method honors the per-report on/off toggle stored in the shared
settings table (see report_config.py). Essential reports (kill switch,
AI rate-limit halt, engine errors, liquidation warning) can never be turned
off and always fire.
"""
import logging
import requests
import psutil
from datetime import datetime, timezone

logger = logging.getLogger("telegram_reporter")


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _bool(v) -> bool:
    return str(v).lower() in ("1", "true", "yes", "on")


class TelegramReporter:
    def __init__(self, bot_token: str, chat_id: str, settings_provider=None):
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.chat_id = chat_id
        # Optional callable returning the current settings dict, used to check
        # whether a given report is enabled before sending. If None, everything
        # is sent (backwards compatible).
        self._settings_provider = settings_provider

    def _enabled(self, report_key: str) -> bool:
        """Check the on/off toggle for a report. Essential reports always
        return True (see report_config). If no provider is wired, allow all."""
        if self._settings_provider is None:
            return True
        try:
            from .report_config import is_enabled
            settings = self._settings_provider() or {}
            return is_enabled(settings, report_key)
        except Exception:
            return True  # fail open: never silently drop due to a config bug

    def _send(self, text: str):
        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=15,
            )
            if not resp.ok:
                logger.warning(f"Telegram send failed: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.warning(f"Telegram send exception: {e}")

    # ============================ lifecycle ============================
    def started(self, lang: str, network: str, symbols: list, model: str, max_leverage, risk_pct, capital):
        if not self._enabled("engine_started"):
            return
        self._send(t(
            "started", lang, network=network, symbols=", ".join(symbols),
            model=model, max_leverage=max_leverage, risk_pct=risk_pct, capital=capital,
        ))

    def strategy_activated(self, lang: str, label: str, key: str):
        """Confirm a strategy change actually took effect in the engine. Sent
        the cycle after the owner switches strategy from the control bot, so a
        silent persona mismatch is always surfaced."""
        if not self._enabled("strategy_activated"):
            return
        self._send(t("strategy_activated", lang, label=label, strat_key=key))

    # ============================== trades ==============================
    def trade_opened(self, lang: str, network: str, **kwargs):
        if not self._enabled("trade_opened"):
            return
        kwargs["side_upper"] = kwargs["side"].upper()
        kwargs["network"] = network
        kwargs["timestamp"] = _now_utc()
        self._send(t("trade_opened", lang, **kwargs))

    def trade_closed(self, lang: str, network: str, **kwargs):
        if not self._enabled("trade_closed"):
            return
        kwargs["side_upper"] = kwargs["side"].upper()
        kwargs["network"] = network
        self._send(t("trade_closed", lang, **kwargs))

    def hold_signal(self, lang: str, symbol: str, confidence: float, reasoning: str,
                    strength: float = None, agreement: str = None):
        """Report a deliberate HOLD/skip with the AI's reasoning, so the owner
        can see *why* no trade was taken (not just silence)."""
        if not self._enabled("hold_signal"):
            return
        extra = ""
        if strength is not None:
            extra += f"\n💪 سیگنال قدرت / Strength: {strength}/100"
        if agreement:
            extra += f"\n🤝 توافق تکنیکال/بنیادی / Agreement: {agreement}"
        self._send(t("hold_signal", lang, symbol=symbol, confidence=confidence,
                     reasoning=reasoning) + extra)

    # ===================== periodic summaries =====================
    def daily_summary(self, lang: str, **kwargs):
        if not self._enabled("daily_summary"):
            return
        self._send(t("daily_summary", lang, **kwargs))

    def weekly_summary(self, lang: str, **kwargs):
        if not self._enabled("weekly_summary"):
            return
        self._send(t("periodic", lang, period="هفتگی" if lang == "fa" else "Weekly", **kwargs))

    def monthly_summary(self, lang: str, **kwargs):
        if not self._enabled("monthly_summary"):
            return
        self._send(t("periodic", lang, period="ماهانه" if lang == "fa" else "Monthly", **kwargs))

    def periodic_status(self, lang: str, **kwargs):
        if not self._enabled("periodic_status"):
            return
        self._send(t("periodic_status", lang, **kwargs))

    def morning_report(self, lang: str, **kwargs):
        if not self._enabled("morning_report"):
            return
        self._send(t("morning_report", lang, **kwargs))

    # ===================== instant alerts =====================
    def liquidation_warn(self, lang, symbol, side, entry, liq, current, distance_pct):
        """ESSENTIAL: never silenced. Fires when a position is close to liquidation."""
        if not self._enabled("liquidation_warn"):
            return
        self._send(t("liquidation_warn", lang, symbol=symbol, side=side.upper(),
                     entry=entry, liq=liq, current=current, dist=distance_pct))

    def drawdown_warn(self, lang, pct, balance, peak):
        if not self._enabled("drawdown_warn"):
            return
        self._send(t("drawdown_warn", lang, pct=pct, balance=balance, peak=peak))

    def tp_sl_near(self, lang, symbol, kind, price, distance_pct):
        if not self._enabled("tp_sl_near"):
            return
        self._send(t("tp_sl_near", lang, symbol=symbol, kind=kind, price=price, dist=distance_pct))

    def funding_high(self, lang, symbol, rate_pct, notional, cost_usd):
        if not self._enabled("funding_high"):
            return
        self._send(t("funding_high", lang, symbol=symbol, rate=rate_pct, notional=notional, cost=cost_usd))

    def deposit(self, lang: str, amount: float, balance: float):
        if not self._enabled("deposit_withdraw"):
            return
        self._send(t("deposit", lang, amount=amount, balance=balance))

    def withdrawal(self, lang: str, amount: float, balance: float):
        if not self._enabled("deposit_withdraw"):
            return
        self._send(t("withdrawal", lang, amount=amount, balance=balance))

    # ===================== analytical =====================
    def best_worst(self, lang, best_sym, best_pnl, worst_sym, worst_pnl, count, win_rate):
        if not self._enabled("best_worst"):
            return
        self._send(t("best_worst", lang, best_sym=best_sym, best_pnl=best_pnl,
                     worst_sym=worst_sym, worst_pnl=worst_pnl, count=count, win_rate=win_rate))

    def fee_funding_report(self, lang, fees, funding, net, period):
        if not self._enabled("fee_funding_report"):
            return
        self._send(t("fee_funding_report", lang, fees=fees, funding=funding, net=net, period=period))

    def strategy_perf(self, lang, rows):
        if not self._enabled("strategy_perf"):
            return
        body = "\n".join(f"• {r['label']}: {r['pnl']:+.2f}$ ({r['count']} معامله)" for r in rows)
        self._send(t("strategy_perf", lang) + "\n" + body)

    def rejected_signals(self, lang, count, examples):
        if not self._enabled("rejected_signals"):
            return
        self._send(t("rejected_signals", lang, count=count, examples=examples))

    # ===================== server health =====================
    def server_health(self, lang, cpu, ram, disk, note=""):
        if not self._enabled("server_health"):
            return
        self._send(t("server_health", lang, cpu=cpu, ram=ram, disk=disk, note=note))

    # ===================== always-on criticals =====================
    def kill_switch(self, lang: str, reason: str, pnl_today: float):
        # essential: no _enabled() guard
        self._send(t("kill_switch", lang, reason=reason, pnl_today=pnl_today))

    def error(self, lang: str, symbol: str, message: str):
        self._send(t("error", lang, symbol=symbol, message=message))

    def ai_rate_limited(self, detail: str):
        """Critical alert: AI provider(s) are fully rate-limited and trading
        has been halted. Sent in both languages so the owner always sees it."""
        try:
            self._send(
                "🛑🛑🛑 <b>AI RATE-LIMITED — TRADING HALTED</b> 🛑🛑🛑\n\n"
                f"{detail}\n\n"
                "The bot has STOPPED trading. Fix it (add a fresh AI key from the "
                "control bot's 🔑 AI keys menu) and restart the engine to resume."
            )
        except Exception as e:
            logger.warning(f"ai_rate_limited send failed: {e}")

    def closeall_done(self, lang: str, symbols: list):
        self._send(t("closeall_done", lang, symbols=", ".join(symbols) if symbols else "-"))

    def inactivity_warn(self, lang, hours):
        if not self._enabled("inactivity_warn"):
            return
        self._send(t("inactivity_warn", lang, hours=hours))

    def ai_cost_report(self, lang, calls, tokens, period, note=""):
        if not self._enabled("ai_cost_report"):
            return
        self._send(t("ai_cost_report", lang, calls=calls, tokens=tokens, period=period, note=note))

    def trade_postmortem(self, lang, symbol, side, entry, exit_px, pnl, reason_entry, reason_exit):
        if not self._enabled("trade_postmortem"):
            return
        self._send(t("trade_postmortem", lang, symbol=symbol, side=side.upper(),
                     entry=entry, exit_px=exit_px, pnl=pnl,
                     reason_entry=reason_entry, reason_exit=reason_exit))

    def recovery_alert(self, lang, peak, current):
        if not self._enabled("recovery_alert"):
            return
        self._send(t("recovery_alert", lang, peak=peak, current=current))

    def trade_pnl_short(self, lang, symbol, side, pnl):
        if not self._enabled("trade_pnl_short"):
            return
        icon = "🟢" if pnl >= 0 else "🔴"
        self._send(f"{icon} {symbol} {side.upper()}: {pnl:+.2f}$")

    def exchange_health(self, lang, ok, detail=""):
        if not self._enabled("exchange_health"):
            return
        self._send(t("exchange_health", lang, ok=ok, detail=detail))

    def whale_alert(self, lang, symbol, side, size_usd, price):
        if not self._enabled("whale_alert"):
            return
        self._send(t("whale_alert", lang, symbol=symbol, side=side, size=size_usd, price=price))

    def loss_streak_warn(self, lang, streak, last_pnl):
        if not self._enabled("loss_streak_warn"):
            return
        self._send(t("loss_streak_warn", lang, streak=streak, last_pnl=last_pnl))


# ---------------------------------------------------------------------------
# Lightweight i18n for reporter messages. Keys mirror core_engine/src/i18n.py
# where they exist; reporter-only keys live here.
# ---------------------------------------------------------------------------
STRINGS = {
    "started": {
        "en": (
            "🚀 Engine started\n"
            "🌐 Network: {network}\n"
            "🎯 Symbols: {symbols}\n"
            "🤖 Model: {model}\n"
            "⚙️ MaxLev: {max_leverage}x | Risk: {risk_pct}% | Capital: ${capital}"
        ),
        "fa": (
            "🚀 موتور معاملاتی روشن شد\n"
            "🌐 شبکه: {network}\n"
            "🎯 نمادها: {symbols}\n"
            "🤖 مدل: {model}\n"
            "⚙️ سقف لوریج: {max_leverage}x | ریسک: {risk_pct}٪ | سرمایه: {capital}$"
        ),
    },
    "strategy_activated": {
        "en": (
            "✅ <b>STRATEGY ACTIVATED</b>\n"
            "━━━━━━━━━━━━━━\n"
            "🧠 {label}\n"
            "🔑 key: {strat_key}\n"
            f"⏱ {_now_utc()}\n"
            "The engine confirmed this persona is now in effect on the next cycle."
        ),
        "fa": (
            "✅ <b>استراتژی فعال شد</b>\n"
            "━━━━━━━━━━━━━━\n"
            "🧠 {label}\n"
            "🔑 کلید: {strat_key}\n"
            f"⏱ {_now_utc()}\n"
            "موتور تایید کرد این شخصیت (persona) از چرخه‌ی بعد اعمال شده."
        ),
    },
    "trade_opened": {
        "en": (
            "🟢 [OPEN] {symbol} | {side_upper}\n"
            "━━━━━━━━━━━━━━\n"
            "💲 Entry: {entry}\n"
            "💰 Size: ${size} | Notional: ${notional}\n"
            "⚡ Leverage: {leverage}x | Margin: ${margin_usd}\n"
            "📊 Capital used: {capital_pct}%\n"
            "🛡 SL: {sl} ({sl_pct}%)\n"
            "🎯 TP: {tp} ({tp_pct}%)\n"
            "⚠️ Risk: {risk_pct}%\n"
            "🤖 Model: {model}\n"
            "🔍 Conf: {confidence}\n"
            "🌐 {network} | ⏱ {timestamp}\n"
            "📝 {reasoning}"
        ),
        "fa": (
            "🟢 [باز شد] {symbol} | {side_upper}\n"
            "━━━━━━━━━━━━━━\n"
            "💲 ورود: {entry}\n"
            "💰 حجم: {size}$ | ارزش: {notional}$\n"
            "⚡ لوریج: {leverage}x | مارجین: {margin_usd}$\n"
            "📊 سرمایه مصرفی: {capital_pct}٪\n"
            "🛡 حد ضرر: {sl} ({sl_pct}٪)\n"
            "🎯 حد سود: {tp} ({tp_pct}٪)\n"
            "⚠️ ریسک: {risk_pct}٪\n"
            "🤖 مدل: {model}\n"
            "🔍 اطمینان: {confidence}\n"
            "🌐 {network} | ⏱ {timestamp}\n"
            "📝 {reasoning}"
        ),
    },
    "trade_closed": {
        "en": (
            "🔴 [CLOSED] {symbol} | {side_upper}\n"
            "━━━━━━━━━━━━━━\n"
            "💵 Exit: {exit_price} | Entry: {entry_price}\n"
            "📊 PnL: {pnl} ({pnl_pct}%)\n"
            "🛡 Reason: {closed_by}\n"
            "⏱ Held: {held_time}\n"
            "━━━━━━━━━━━━━━\n"
            "🌐 {network}"
        ),
        "fa": (
            "🔴 [بسته شد] {symbol} | {side_upper}\n"
            "━━━━━━━━━━━━━━\n"
            "💵 خروج: {exit_price} | ورود: {entry_price}\n"
            "📊 سود/زیان: {pnl} ({pnl_pct}٪)\n"
            "🛡 دلیل بسته شدن: {closed_by}\n"
            "⏱ مدت نگهداری: {held_time}\n"
            "━━━━━━━━━━━━━━\n"
            "🌐 {network}"
        ),
    },
    "hold_signal": {
        "en": "⚪ [HOLD] {symbol}\nConf {confidence} < threshold → skipped\n{reasoning}",
        "fa": "⚪ [بدون معامله] {symbol}\nاطمینان {confidence} کمتر از حد نصاب → رد شد\n{reasoning}",
    },
    "deposit": {
        "en": "💰 Deposit detected: +{amount} USD\nNew balance: {balance} USD",
        "fa": "💰 واریز شناسایی شد: +{amount} دلار\nموجودی جدید: {balance} دلار",
    },
    "withdrawal": {
        "en": "🏧 Withdrawal detected: -{amount} USD\nNew balance: {balance} USD",
        "fa": "🏧 برداشت شناسایی شد: -{amount} دلار\nموجودی جدید: {balance} دلار",
    },
    "daily_summary": {
        "en": (
            "📊 <b>Daily Summary</b>\n"
            "━━━━━━━━━━━━━━\n"
            "📈 Trades: {count}\n"
            "✅ Win rate: {win_rate}%\n"
            "💸 PnL: {pnl} USD\n"
            "💼 Open positions: {open_positions}\n"
            "💵 Balance: {balance} USD\n"
            "🧠 Strategy: {strategy}"
        ),
        "fa": (
            "📊 <b>خلاصه روزانه</b>\n"
            "━━━━━━━━━━━━━━\n"
            "📈 تعداد معاملات: {count}\n"
            "✅ نرخ برد: {win_rate}٪\n"
            "💸 سود/زیان: {pnl} دلار\n"
            "💼 پوزیشن‌های باز: {open_positions}\n"
            "💵 موجودی: {balance} دلار\n"
            "🧠 استراتژی: {strategy}"
        ),
    },
    "periodic": {
        "en": (
            "📆 <b>{period} Summary</b>\n"
            "━━━━━━━━━━━━━━\n"
            "📈 Trades: {count}\n"
            "✅ Win rate: {win_rate}%\n"
            "💸 PnL: {pnl} USD\n"
            "💼 Open positions: {open_positions}\n"
            "💵 Balance: {balance} USD\n"
            "🧠 Strategy: {strategy}"
        ),
        "fa": (
            "📆 <b>خلاصه {period}</b>\n"
            "━━━━━━━━━━━━━━\n"
            "📈 تعداد معاملات: {count}\n"
            "✅ نرخ برد: {win_rate}٪\n"
            "💸 سود/زیان: {pnl} دلار\n"
            "💼 پوزیشن‌های باز: {open_positions}\n"
            "💵 موجودی: {balance} دلار\n"
            "🧠 استراتژی: {strategy}"
        ),
    },
    "periodic_status": {
        "en": (
            "⏰ <b>Status Update</b>\n"
            "━━━━━━━━━━━━━━\n"
            "💵 Balance: {balance} USD\n"
            "💼 Open positions: {open_positions}\n"
            "📊 Unrealized PnL: {upnl} USD\n"
            "🧠 Strategy: {strategy}"
        ),
        "fa": (
            "⏰ <b>به‌روزرسانی وضعیت</b>\n"
            "━━━━━━━━━━━━━━\n"
            "💵 موجودی: {balance} دلار\n"
            "💼 پوزیشن‌های باز: {open_positions}\n"
            "📊 سود/زیان باز: {upnl} دلار\n"
            "🧠 استراتژی: {strategy}"
        ),
    },
    "morning_report": {
        "en": (
            "🌅 <b>Morning Report</b>\n"
            "━━━━━━━━━━━━━━\n"
            "💵 Balance: {balance} USD\n"
            "💼 Open positions: {open_positions}\n"
            "📊 Unrealized PnL: {upnl} USD\n"
            "🌐 Network: {network}\n"
            "🧠 Strategy: {strategy}"
        ),
        "fa": (
            "🌅 <b>گزارش صبحگاهی</b>\n"
            "━━━━━━━━━━━━━━\n"
            "💵 موجودی: {balance} دلار\n"
            "💼 پوزیشن‌های باز: {open_positions}\n"
            "📊 سود/زیان باز: {upnl} دلار\n"
            "🌐 شبکه: {network}\n"
            "🧠 استراتژی: {strategy}"
        ),
    },
    "liquidation_warn": {
        "en": (
            "🚨 <b>LIQUIDATION RISK</b> 🚨\n"
            "━━━━━━━━━━━━━━\n"
            "🔴 {symbol} {side}\n"
            "💲 Entry: {entry}\n"
            "💀 Liquidation: {liq}\n"
            "📍 Current: {current}\n"
            "📏 Distance: {dist}%\n"
            "⚠️ Close or add margin now!"
        ),
        "fa": (
            "🚨 <b>خطر لیکویید شدن</b> 🚨\n"
            "━━━━━━━━━━━━━━\n"
            "🔴 {symbol} {side}\n"
            "💲 ورود: {entry}\n"
            "💀 لیکویید: {liq}\n"
            "📍 فعلی: {current}\n"
            "📏 فاصله: {dist}٪\n"
            "⚠️ همین الان ببند یا مارجین اضافه کن!"
        ),
    },
    "drawdown_warn": {
        "en": (
            "📉 <b>DRAWDOWN ALERT</b>\n"
            "━━━━━━━━━━━━━━\n"
            "💸 Equity dropped {pct}% from peak\n"
            "📊 Current: {balance} USD\n"
            "🔝 Peak: {peak} USD"
        ),
        "fa": (
            "📉 <b>هشدار افت موجودی</b>\n"
            "━━━━━━━━━━━━━━\n"
            "💸 سرمایه {pct}٪ از سقف افت کرد\n"
            "📊 فعلی: {balance} دلار\n"
            "🔝 سقف: {peak} دلار"
        ),
    },
    "tp_sl_near": {
        "en": (
            "🎯 <b>TP/SL APPROACHING</b>\n"
            "━━━━━━━━━━━━━━\n"
            "{symbol}: {kind} at {price}\n"
            "📏 Distance: {dist}%"
        ),
        "fa": (
            "🎯 <b>نزدیک شدن به TP/SL</b>\n"
            "━━━━━━━━━━━━━━\n"
            "{symbol}: {kind} روی {price}\n"
            "📏 فاصله: {dist}٪"
        ),
    },
    "funding_high": {
        "en": (
            "💸 <b>HIGH FUNDING</b>\n"
            "━━━━━━━━━━━━━━\n"
            "{symbol}: {rate}% per 8h\n"
            "📊 Notional: {notional} USD\n"
            "💰 Cost/8h: {cost} USD"
        ),
        "fa": (
            "💸 <b>فاندینگ بالا</b>\n"
            "━━━━━━━━━━━━━━\n"
            "{symbol}: {rate}٪ هر ۸ ساعت\n"
            "📊 ارزش: {notional} دلار\n"
            "💰 هزینه/۸ساعت: {cost} دلار"
        ),
    },
    "best_worst": {
        "en": (
            "🏆 <b>Best / Worst Trades</b>\n"
            "━━━━━━━━━━━━━━\n"
            "🥇 Best: {best_sym} {best_pnl:+.2f}$\n"
            "🥉 Worst: {worst_sym} {worst_pnl:+.2f}$\n"
            "📈 Total: {count} trades | Win rate {win_rate}%"
        ),
        "fa": (
            "🏆 <b>بهترین / بدترین معامله</b>\n"
            "━━━━━━━━━━━━━━\n"
            "🥇 بهترین: {best_sym} {best_pnl:+.2f}$\n"
            "🥉 بدترین: {worst_sym} {worst_pnl:+.2f}$\n"
            "📈 جمعاً: {count} معامله | نرخ برد {win_rate}٪"
        ),
    },
    "fee_funding_report": {
        "en": (
            "🧾 <b>Fees & Funding ({period})</b>\n"
            "━━━━━━━━━━━━━━\n"
            "💸 Fees paid: {fees:.4f}$\n"
            "💰 Funding: {funding:+.4f}$\n"
            "📊 Net cost: {net:.4f}$"
        ),
        "fa": (
            "🧾 <b>کارمزد و فاندینگ ({period})</b>\n"
            "━━━━━━━━━━━━━━\n"
            "💸 کارمزد پرداختی: {fees:.4f}$\n"
            "💰 فاندینگ: {funding:+.4f}$\n"
            "📊 هزینه خالص: {net:.4f}$"
        ),
    },
    "strategy_perf": {
        "en": "🧠 <b>Strategy Performance</b>\n━━━━━━━━━━━━━━",
        "fa": "🧠 <b>عملکرد استراتژی‌ها</b>\n━━━━━━━━━━━━━━",
    },
    "rejected_signals": {
        "en": (
            "🔍 <b>Rejected Signals</b>\n"
            "━━━━━━━━━━━━━━\n"
            "📋 {count} signals below threshold\n"
            "{examples}"
        ),
        "fa": (
            "🔍 <b>سیگنال‌های رد شده</b>\n"
            "━━━━━━━━━━━━━━\n"
            "📋 {count} سیگنال زیر حد نصاب\n"
            "{examples}"
        ),
    },
    "server_health": {
        "en": (
            "🖥 <b>SERVER HEALTH ALERT</b>\n"
            "━━━━━━━━━━━━━━\n"
            "🔥 CPU: {cpu}%\n"
            "🧠 RAM: {ram}%\n"
            "💽 Disk: {disk}%\n"
            "{note}"
        ),
        "fa": (
            "🖥 <b>هشدار سلامت سرور</b>\n"
            "━━━━━━━━━━━━━━\n"
            "🔥 پردازنده: {cpu}٪\n"
            "🧠 حافظه: {ram}٪\n"
            "💽 دیسک: {disk}٪\n"
            "{note}"
        ),
    },
    "kill_switch": {
        "en": (
            "🚨 KILL SWITCH TRIPPED\n"
            "━━━━━━━━━━━━━━\n"
            "❌ Reason: {reason}\n"
            "💸 Today PnL: {pnl_today}\n"
            "🔒 Bot auto-paused. Use /resume after review.\n"
            "━━━━━━━━━━━━━━"
        ),
        "fa": (
            "🚨 توقف اضطراری فعال شد\n"
            "━━━━━━━━━━━━━━\n"
            "❌ دلیل: {reason}\n"
            "💸 سود/زیان امروز: {pnl_today}\n"
            "🔒 بات متوقف شد. بعد از بررسی از /resume استفاده کن.\n"
            "━━━━━━━━━━━━━━"
        ),
    },
    "error": {
        "en": "⚠️ ERROR | {symbol}\n━━━━━━━━━━━━\n❌ {message}\n🔧 Bot continues next cycle.\n━━━━━━━━━━━━",
        "fa": "⚠️ خطا | {symbol}\n━━━━━━━━━━━━\n❌ {message}\n🔧 بات در چرخه‌ی بعدی ادامه می‌ده.\n━━━━━━━━━━━━",
    },
    "closeall_done": {
        "en": "🔒 Closed all positions: {symbols}",
        "fa": "🔒 همه‌ی پوزیشن‌ها بسته شدن: {symbols}",
    },
    "inactivity_warn": {
        "en": (
            "💤 <b>INACTIVITY ALERT</b>\n"
            "━━━━━━━━━━━━━━\n"
            "⏳ No position opened for {hours} hours\n"
            "🤖 Bot is active but no trade triggered.\n"
            "Check AI keys / strategy signal threshold."
        ),
        "fa": (
            "💤 <b>هشدار عدم فعالیت</b>\n"
            "━━━━━━━━━━━━━━\n"
            "⏳ {hours} ساعت هیچ پوزیشنی باز نشد\n"
            "🤖 بات فعاله ولی هیچ معامله‌ای تریگر نشد.\n"
            "کلیدهای هوش / آستانه سیگنال رو چک کن."
        ),
    },
    "ai_cost_report": {
        "en": (
            "🤖 <b>AI USAGE ({period})</b>\n"
            "━━━━━━━━━━━━━━\n"
            "📞 API calls: {calls}\n"
            "🔤 Tokens: {tokens}\n"
            "{note}"
        ),
        "fa": (
            "🤖 <b>مصرف هوش ({period})</b>\n"
            "━━━━━━━━━━━━━━\n"
            "📞 تعداد کال: {calls}\n"
            "🔤 توکن: {tokens}\n"
            "{note}"
        ),
    },
    "trade_postmortem": {
        "en": (
            "🔬 <b>TRADE POST-MORTEM</b>\n"
            "━━━━━━━━━━━━━━\n"
            "{symbol} {side}\n"
            "💲 Entry: {entry} → Exit: {exit_px}\n"
            "📊 PnL: {pnl:+.2f}$\n"
            "🟢 Entry reason: {reason_entry}\n"
            "🔴 Exit reason: {reason_exit}"
        ),
        "fa": (
            "🔬 <b>بررسی دقیق معامله</b>\n"
            "━━━━━━━━━━━━━━\n"
            "{symbol} {side}\n"
            "💲 ورود: {entry} → خروج: {exit_px}\n"
            "📊 سود/زیان: {pnl:+.2f}$\n"
            "🟢 دلیل ورود: {reason_entry}\n"
            "🔴 دلیل خروج: {reason_exit}"
        ),
    },
    "recovery_alert": {
        "en": (
            "🎉 <b>RECOVERED FROM DRAWDOWN</b>\n"
            "━━━━━━━━━━━━━━\n"
            "💵 Back to peak: {current}$ (was {peak}$)\n"
            "📈 Equity fully recovered!"
        ),
        "fa": (
            "🎉 <b>خارج شدن از ضرر</b>\n"
            "━━━━━━━━━━━━━━\n"
            "💵 برگشت به سقف: {current}$ (قبلاً {peak}$)\n"
            "📈 سرمایه کاملاً ریکاوری شد!"
        ),
    },
    "exchange_health": {
        "en": (
            "🔌 <b>EXCHANGE CONNECTION</b>\n"
            "━━━━━━━━━━━━━━\n"
            "{ok}\n"
            "{detail}"
        ),
        "fa": (
            "🔌 <b>ارتباط صرافی</b>\n"
            "━━━━━━━━━━━━━━\n"
            "{ok}\n"
            "{detail}"
        ),
    },
    "whale_alert": {
        "en": (
            "🐋 <b>WHALE ALERT</b>\n"
            "━━━━━━━━━━━━━━\n"
            "{symbol} {side} {size}$ @ {price}\n"
            "👀 Large position detected on your market"
        ),
        "fa": (
            "🐋 <b>هشدار نهنگ</b>\n"
            "━━━━━━━━━━━━━━\n"
            "{symbol} {side} {size}$ @ {price}\n"
            "👀 پوزیشن بزرگ روی بازار شما شناسایی شد"
        ),
    },
    "loss_streak_warn": {
        "en": (
            "📉 <b>LOSS STREAK</b>\n"
            "━━━━━━━━━━━━━━\n"
            "🔴 {streak} consecutive losses\n"
            "💸 Last: {last_pnl:+.2f}$\n"
            "⚠️ Consider pausing / reviewing strategy"
        ),
        "fa": (
            "📉 <b>استریک باخت</b>\n"
            "━━━━━━━━━━━━━━\n"
            "🔴 {streak} باخت پشت سر هم\n"
            "💸 آخری: {last_pnl:+.2f}$\n"
            "⚠️ بهتره مکث کنی / استراتژی رو چک کنی"
        ),
    },
}


def t(key: str, lang: str, **kwargs) -> str:
    lang = lang if lang in ("en", "fa") else "en"
    template = STRINGS.get(key, {}).get(lang, STRINGS.get(key, {}).get("en", key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template
