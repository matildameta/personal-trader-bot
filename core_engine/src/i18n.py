"""
All internal code, logs, prompts, and variable names stay in English.
This file is the ONLY place user-facing Telegram text is defined, in two
languages. `language` comes from shared settings and can be flipped
anytime from the control_bot menu.
"""

STRINGS = {
    "trade_opened": {
        "en": (
            "🟢 [OPEN] {symbol} | {side_upper}\n"
            "━━━━━━━━━━━━━━\n"
            "💰 Entry: {entry}\n"
            "📦 Size: {size} {symbol} (${notional})\n"
            "💵 Margin used: ${margin_usd} ({capital_pct}% of capital)\n"
            "⚡ Leverage: {leverage}x\n"
            "🛡 SL: {sl} ({sl_pct}%)\n"
            "🎯 TP: {tp} ({tp_pct}%)\n"
            "⚠️ Risk: {risk_pct}% of capital\n"
            "🤖 Model: {model}\n"
            "📈 Conf: {confidence}\n"
            "🧠 Why: {reasoning}\n"
            "━━━━━━━━━━━━━━\n"
            "🌐 {network} | {timestamp}"
        ),
        "fa": (
            "🟢 [باز شد] {symbol} | {side_upper}\n"
            "━━━━━━━━━━━━━━\n"
            "💰 ورود: {entry}\n"
            "📦 حجم: {size} {symbol} ({notional}$)\n"
            "💵 مبلغ واردشده: {margin_usd}$ ({capital_pct}٪ از سرمایه)\n"
            "⚡ لوریج: {leverage}x\n"
            "🛡 حد ضرر: {sl} ({sl_pct}٪)\n"
            "🎯 حد سود: {tp} ({tp_pct}٪)\n"
            "⚠️ ریسک: {risk_pct}٪ از سرمایه\n"
            "🤖 مدل: {model}\n"
            "📈 اطمینان: {confidence}\n"
            "🧠 دلیل: {reasoning}\n"
            "━━━━━━━━━━━━━━\n"
            "🌐 {network} | {timestamp}"
        ),
    },
    "trade_closed": {
        "en": (
            "🔴 [CLOSE] {symbol} | {side_upper}\n"
            "━━━━━━━━━━━━━━\n"
            "💵 Exit: {exit_price} | Entry: {entry_price}\n"
            "📊 PnL: {pnl} ({pnl_pct}%)\n"
            "🛡 Closed by: {closed_by}\n"
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
        "en": "📊 Daily summary\nTrades: {count}\nWin rate: {win_rate}%\nPnL: {pnl} USD\nBalance: {balance} USD",
        "fa": "📊 خلاصه روزانه\nتعداد معاملات: {count}\nنرخ برد: {win_rate}٪\nسود/زیان: {pnl} دلار\nموجودی: {balance} دلار",
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
    "error": {
        "en": "⚠️ ERROR | {symbol}\n━━━━━━━━━━━━\n❌ {message}\n🔧 Bot continues next cycle.\n━━━━━━━━━━━━",
        "fa": "⚠️ خطا | {symbol}\n━━━━━━━━━━━━\n❌ {message}\n🔧 بات در چرخه‌ی بعدی ادامه می‌ده.\n━━━━━━━━━━━━",
    },
    "closeall_done": {
        "en": "🔒 Closed all positions: {symbols}",
        "fa": "🔒 همه‌ی پوزیشن‌ها بسته شدن: {symbols}",
    },
}


def t(key: str, lang: str, **kwargs) -> str:
    lang = lang if lang in ("en", "fa") else "en"
    template = STRINGS.get(key, {}).get(lang, STRINGS.get(key, {}).get("en", key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template
