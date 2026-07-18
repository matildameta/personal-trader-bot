"""Central registry of every reporter feature + its on/off state.

Each report has a key, a Persian/English label, a default state, and an
`essential` flag. Essential reports (kill switch, rate-limit halt, errors,
liquidation warning) can NEVER be turned off from the panel — they always
fire, because silencing them could cost real money.

State is stored in the shared settings table under keys prefixed with
`report_` so both the engine (which reads to decide whether to send) and the
control bot (which toggles) see the same values.
"""

# key -> {label_fa, label_en, default, essential}
REPORTS = {
    # ---- lifecycle ----
    "engine_started":   {"fa": "🚀 روشن شدن موتور",           "en": "🚀 Engine started",        "default": True,  "essential": False},
    "strategy_activated": {"fa": "✅ فعال شدن استراتژی",       "en": "✅ Strategy activated",     "default": True,  "essential": False},
    # ---- trades ----
    "trade_opened":     {"fa": "🟢 باز شدن معامله",           "en": "🟢 Trade opened",          "default": True,  "essential": False},
    "trade_closed":     {"fa": "🔴 بسته شدن معامله",          "en": "🔴 Trade closed",          "default": True,  "essential": False},
    "hold_signal":      {"fa": "⚪ سیگنال HOLD (بدون معامله)", "en": "⚪ HOLD signal",           "default": True,  "essential": False},
    # ---- periodic ----
    "daily_summary":    {"fa": "📊 خلاصه روزانه",             "en": "📊 Daily summary",         "default": True,  "essential": False},
    "weekly_summary":   {"fa": "📆 خلاصه هفتگی",              "en": "📆 Weekly summary",        "default": True,  "essential": False},
    "monthly_summary":  {"fa": "🗓 خلاصه ماهانه",             "en": "🗓 Monthly summary",       "default": True,  "essential": False},
    "periodic_status":  {"fa": "⏰ گزارش دوره‌ای وضعیت",      "en": "⏰ Periodic status",       "default": False, "essential": False},
    "morning_report":   {"fa": "🌅 گزارش صبحگاهی",            "en": "🌅 Morning report",        "default": False, "essential": False},
    # ---- instant alerts ----
    "liquidation_warn": {"fa": "⚠️ هشدار نزدیکی لیکویید",     "en": "⚠️ Liquidation warning",   "default": True,  "essential": True},
    "drawdown_warn":    {"fa": "📉 هشدار افت موجودی",         "en": "📉 Drawdown warning",      "default": True,  "essential": False},
    "tp_sl_near":       {"fa": "🎯 نزدیک شدن به TP/SL",       "en": "🎯 Near TP/SL",            "default": False, "essential": False},
    "funding_high":     {"fa": "💸 هشدار فاندینگ بالا",       "en": "💸 High funding alert",    "default": False, "essential": False},
    "deposit_withdraw": {"fa": "💰 واریز/برداشت",             "en": "💰 Deposit/Withdraw",      "default": True,  "essential": False},
    # ---- analytical ----
    "best_worst":       {"fa": "🏆 بهترین/بدترین معامله",     "en": "🏆 Best/Worst trade",      "default": True,  "essential": False},
    "fee_funding_report": {"fa": "🧾 گزارش کارمزد+فاندینگ",   "en": "🧾 Fee+Funding report",    "default": True,  "essential": False},
    "strategy_perf":    {"fa": "🧠 عملکرد استراتژی‌ها",       "en": "🧠 Strategy performance",  "default": False, "essential": False},
    "rejected_signals": {"fa": "🔍 سیگنال‌های رد شده",        "en": "🔍 Rejected signals",      "default": False, "essential": False},
    # ---- server health ----
    "server_health":    {"fa": "🖥 هشدار سلامت سرور",         "en": "🖥 Server health alert",   "default": True,  "essential": False},
    # ---- always-on criticals (no toggle) ----
    "kill_switch":      {"fa": "🚨 توقف اضطراری",             "en": "🚨 Kill switch",           "default": True,  "essential": True},
    "ai_rate_limited":  {"fa": "🛑 توقف به‌خاطر rate-limit هوش","en": "🛑 AI rate-limit halt",   "default": True,  "essential": True},
    "engine_error":     {"fa": "⚠️ خطای موتور",              "en": "⚠️ Engine error",          "default": True,  "essential": True},
    # ---- NEW alerts (single "hشدارها" key group) ----
    "inactivity_warn":  {"fa": "💤 هشدار عدم فعالیت",         "en": "💤 Inactivity alert",      "default": True,  "essential": False},
    "ai_cost_report":   {"fa": "🤖 گزارش هزینه هوش",          "en": "🤖 AI cost report",        "default": True,  "essential": False},
    "trade_postmortem": {"fa": "🔬 بررسی دقیق معامله",        "en": "🔬 Trade post-mortem",     "default": True,  "essential": False},
    "recovery_alert":   {"fa": "🎉 هشدار ریکاوری",            "en": "🎉 Recovery alert",        "default": True,  "essential": False},
    "trade_pnl_short":  {"fa": "⚡ اعلام سود/ضرر کوتاه",       "en": "⚡ Short P&L ping",         "default": True,  "essential": False},
    "exchange_health":  {"fa": "🔌 هشدار ارتباط صرافی",       "en": "🔌 Exchange health",       "default": True,  "essential": False},
    "whale_alert":      {"fa": "🐋 هشدار نهنگ",              "en": "🐋 Whale alert",            "default": True,  "essential": False},
    "loss_streak_warn": {"fa": "📉 هشدار استریک باخت",       "en": "📉 Loss-streak alert",     "default": True,  "essential": False},
}

# keys that belong to the dedicated "هشدارها" (alerts) sub-panel
ALERT_KEYS = [
    "liquidation_warn", "drawdown_warn", "tp_sl_near", "funding_high",
    "inactivity_warn", "ai_cost_report", "trade_postmortem", "recovery_alert",
    "trade_pnl_short", "exchange_health", "whale_alert", "loss_streak_warn",
]


def setting_key(report_key: str) -> str:
    return f"report_{report_key}"


def is_enabled(settings: dict, report_key: str) -> bool:
    """True if this report should be sent. Essential reports are always on."""
    meta = REPORTS.get(report_key)
    if meta is None:
        return True  # unknown key: fail open (don't silently drop)
    if meta["essential"]:
        return True
    val = settings.get(setting_key(report_key))
    if val is None:
        return meta["default"]
    return bool(val)


def default_settings() -> dict:
    """Return the report_* defaults to seed the settings table with."""
    return {setting_key(k): v["default"] for k, v in REPORTS.items() if not v["essential"]}
