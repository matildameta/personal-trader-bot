STRINGS = {
    "welcome": {
        "en": (
            "👋 <b>Welcome to your Trading Bot Control Panel</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "This bot fully controls your AI-driven Hyperliquid trading engine:\n"
            "• Set capital, risk, leverage and loss limits\n"
            "• Pick the analysis strategy (AI runs a fixed 4-stage combined pipeline)\n"
            "• Choose which symbols/timeframes to trade\n"
            "• Get P&L reports (daily/weekly/monthly) and live status\n"
            "• Pause, resume, close all positions, or restart — anytime\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "Tap a button below, or send /help for the full command list."
        ),
        "fa": (
            "👋 <b>به پنل کنترل بات معاملاتی خودت خوش اومدی</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "با این بات می‌تونی کل موتور معاملاتی هوشمند روی هایپرلیکوئید رو کنترل کنی:\n"
            "• تعیین سرمایه، ریسک، لوریج و سقف ضرر\n"
            "• انتخاب استراتژی تحلیل (هوش مصنوعی یک پایپ‌لاین ۴ مرحله‌ای ترکیبی و ثابته)\n"
            "• انتخاب نمادها و تایم‌فریم‌های معاملاتی\n"
            "• گزارش سود/زیان روزانه، هفتگی، ماهانه و وضعیت لحظه‌ای\n"
            "• توقف، ازسرگیری، بستن همه‌ی پوزیشن‌ها یا ری‌استارت — هر وقت خواستی\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "از دکمه‌های پایین استفاده کن، یا /help رو بزن برای لیست کامل دستورات."
        ),
    },
    "menu_title": {"en": "⚙️ Trading Bot Control Panel", "fa": "⚙️ پنل کنترل بات معاملاتی"},
    "btn_capital": {"en": "💵 Capital: ${v}", "fa": "💵 سرمایه: {v}$"},
    "btn_leverage": {"en": "📈 Max leverage: {v}x", "fa": "📈 سقف لوریج: {v}x"},
    "btn_risk": {"en": "⚠️ Risk/trade {v}%", "fa": "⚠️ ریسک/معامله {v}٪"},
    "btn_daily_loss": {"en": "📉 Daily loss {v}%", "fa": "📉 ضرر روزانه {v}٪"},
    "btn_consec_losses": {"en": "🔴 Consec losses {v}", "fa": "🔴 ضرر متوالی {v}"},
    "btn_language": {"en": "🌐 Language: {v}", "fa": "🌐 زبان: {v}"},
    "btn_pause": {"en": "⏸ Pause bot", "fa": "⏸ توقف بات"},
    "btn_resume": {"en": "▶️ Resume bot", "fa": "▶️ فعال کردن بات"},
    "btn_report": {"en": "📊 Recent trades", "fa": "📊 معاملات اخیر"},
    "btn_logs": {"en": "📝 Recent logs", "fa": "📝 لاگ‌های اخیر"},
    "btn_status": {"en": "📋 Status", "fa": "📋 وضعیت"},
    "btn_pnl": {"en": "💹 P&L report", "fa": "💹 گزارش سود/زیان"},
    "btn_ai_keys": {"en": "🔑 AI keys", "fa": "🔑 کلیدهای هوش مصنوعی"},
    "btn_ai_test": {"en": "🔍 Test AI tokens", "fa": "🔍 تست توکن‌های هوش"},
    "btn_strategy": {"en": "🧠 Strategy", "fa": "🧠 استراتژی"},
    "btn_symbols": {"en": "🎯 Symbols", "fa": "🎯 نمادها"},
    "btn_settings": {"en": "🗂 All settings", "fa": "🗂 همه‌ی تنظیمات"},
    "btn_closeall": {"en": "🚨 Close all positions", "fa": "🚨 بستن همه‌ی پوزیشن‌ها"},
    "btn_restart": {"en": "🔄 Restart bot + engine", "fa": "🔄 ری‌استارت بات و موتور"},
    "btn_set_apikey": {"en": "🔑 Set exchange key (Hyperliquid)", "fa": "🔑 تنظیم کلید صرافی (هایپرلیکوئید)"},
    "btn_restart_ctl": {"en": "🎛 Restart control bot", "fa": "🎛 ری‌استارت بات کنترلر"},
    "btn_pnl_hourly": {"en": "⏱ Hourly", "fa": "⏱ ساعتی"},
    "btn_pnl_daily": {"en": "📅 Daily", "fa": "📅 روزانه"},
    "btn_pnl_weekly": {"en": "📆 Weekly", "fa": "📆 هفتگی"},
    "btn_pnl_monthly": {"en": "🗓 Monthly", "fa": "🗓 ماهانه"},
    "btn_network_testnet": {"en": "🧪 Testnet", "fa": "🧪 تست‌نت"},
    "btn_network_mainnet": {"en": "🔴 Mainnet", "fa": "🔴 مین‌نت"},
    "network_switched": {"en": "🌐 Network switched to {network}. Engine will restart to apply.", "fa": "🌐 شبکه به {network} تغییر کرد. موتور برای اعمال ری‌استارت می‌شود."},
    "mainnet_warning": {"en": "⚠️ MAINNET ACTIVE — Real funds at risk!", "fa": "⚠️ مین‌نت فعال است — سرمایه واقعی در خطر!"},
    "enter_new_value": {"en": "Send the new value as a message.", "fa": "مقدار جدید رو به‌صورت پیام بفرست."},
    "value_updated": {"en": "⚙️ SETTING UPDATED\n{key} → {value}\n✅ Applied next cycle (no restart needed)",
                       "fa": "⚙️ تنظیمات تغییر کرد\n{key} → {value}\n✅ از چرخه‌ی بعدی اعمال میشه (نیاز به ری‌استارت نیست)"},
    "invalid_number": {"en": "Please send a positive number.", "fa": "لطفاً یک عدد مثبت بفرست."},
    "leverage_too_high": {"en": "Leverage cap can't exceed {max}x for safety.", "fa": "سقف لوریج نمی‌تونه بیشتر از {max}x باشه (برای ایمنی)."},
    "paused": {"en": "⏸ Bot paused. Open positions stay open; no new entries.", "fa": "⏸ بات متوقف شد. پوزیشن‌های باز دست‌نخورده می‌مونن؛ معامله‌ی جدیدی باز نمیشه."},
    "resumed": {"en": "▶️ Bot resumed.", "fa": "▶️ بات فعال شد."},
    "no_trades": {"en": "No trades yet.", "fa": "هنوز معامله‌ای ثبت نشده."},
    "no_positions": {"en": "No open positions.", "fa": "پوزیشن باز وجود نداره."},
    "unauthorized": {"en": "Not authorized.", "fa": "دسترسی مجاز نیست."},
    "confirm_closeall": {"en": "⚠️ This will close ALL open positions immediately. Are you sure?", "fa": "⚠️ این کار همه‌ی پوزیشن‌های باز رو فوراً می‌بنده. مطمئنی؟"},
    "confirm_resume": {"en": "Resume trading after a kill-switch or manual pause?", "fa": "معاملات بعد از توقف اضطراری یا دستی از سر گرفته بشه؟"},
    "confirm_restart": {"en": "⚠️ This restarts both the control bot and the trading engine process. Continue?",
                         "fa": "⚠️ این کار هم بات کنترلی و هم موتور معاملاتی رو ری‌استارت می‌کنه. ادامه بدم؟"},
    "confirm_restart_ctl": {"en": "⚠️ This restarts ONLY the control bot (not the trading engine). Continue?",
                             "fa": "⚠️ این کار فقط بات کنترلر رو ری‌استارت می‌کنه (نه موتور معاملاتی). ادامه بدم؟"},
    "restart_ctl_waiting": {"en": "⏳ Restarting control bot...\n\nPlease wait, the bot will notify you when it's back online.\n\n⏱ ETA: ~5-10s",
                            "fa": "⏳ در حال ری‌استارت بات کنترلر...\n\nلطفاً صبر کن، بات وقتی آنلاین شد بهت اطلاع میده.\n\n⏱ زمان تقریبی: ~۵-۱۰ ثانیه"},
    "restart_ctl_online": {"en": "✅ Control bot is back online!\n\nThe trading engine is still running untouched. Use /start to open the menu.",
                           "fa": "✅ بات کنترلر دوباره آنلاین شد!\n\nموتور معاملاتی دست‌نخورده در حال اجراست. برای باز کردن منو /start بزن."},
    "ask_setkey": {"en": "🔑 Choose which network to set the Hyperliquid private key for:",
                   "fa": "🔑 برای کدوم شبکه کلید خصوصی (private key) هایپرلیکوید رو می‌خوای ست کنی؟"},
    "ask_setkey_network": {"en": "🔑 Send your Hyperliquid secret key for <b>{network}</b> as a message.\n\nFormat: 0x... (64 hex chars after 0x)\n\nThe bot will save it to the engine config and restart the engine to apply.\n\n⚠️ Never share this with anyone else — it controls your funds.",
                   "fa": "🔑 کلید خصوصی (private key) هایپرلیکوید برای <b>{network}</b> رو به‌صورت پیام بفرست.\n\nفرمت: 0x... (۶۴ کاراکتر هگز بعد از 0x)\n\nبات اونو توی کانفیگ موتور ذخیره می‌کنه و موتور رو ری‌استارت می‌کنه تا اعمال بشه.\n\n⚠️ اینو با هیچ‌کس دیگه به اشتراک نذار — کنترل سرمایه‌ت دستشه."},
    "invalid_key": {"en": "❌ Invalid key format. Expected 0x followed by 64 hex chars.", "fa": "❌ فرمت کلید اشتباهه. باید با 0x شروع بشه و ۶۴ کاراکتر هگز داشته باشه."},
    "key_saved": {"en": "✅ API key saved! The engine will restart to apply it.", "fa": "✅ کلید API ذخیره شد! موتور ری‌استارت می‌شه تا اعمال بشه."},
    "value_updated_short": {"en": "✅ Saved successfully", "fa": "✅ با موفقیت ذخیره شد"},
    "saving": {"en": "💾 Saving...", "fa": "💾 در حال ذخیره..."},
    "saved_short": {"en": "✅ Saved", "fa": "✅ ذخیره شد"},
    "pnl_report_title": {"en": "💹 <b>P&L Report</b>\nSelect a time window (data from Hyperliquid):", "fa": "💹 <b>گزارش سود/زیان</b>\nبازه زمانی رو انتخاب کن (داده‌ها از هایپرلیکوید):"},
    "restart_queued": {"en": "🔄 Restarting... the control bot will reconnect in a few seconds; the engine restarts on its next cycle check.",
                        "fa": "🔄 در حال ری‌استارت... بات کنترلی طی چند ثانیه دوباره وصل میشه؛ موتور معاملاتی هم در بررسی بعدی ری‌استارت می‌شه."},
    "network_switched": {"en": "🌐 Network switched to <b>{network}</b>\n\n✅ Change applied — both bots are restarting.", "fa": "🌐 شبکه به <b>{network}</b> تغییر کرد\n\n✅ تغییر اعمال شد — هر دو بات در حال ری‌استارت هستند."},
    "yes": {"en": "✅ Yes", "fa": "✅ بله"},
    "no": {"en": "❌ Cancel", "fa": "❌ لغو"},
    "ok": {"en": "✅ OK", "fa": "✅ تایید"},
    "btn_add_symbol": {"en": "➕ Add symbol", "fa": "➕ افزودن نماد"},
    "btn_remove_symbol": {"en": "➖ Remove symbol", "fa": "➖ حذف نماد"},
    "btn_back": {"en": "↩️ Back", "fa": "↩️ برگشت"},
    "symbols_active": {"en": "🎯 <b>Active symbols</b>\n{choices}", "fa": "🎯 <b>نمادهای فعال</b>\n{choices}"},
    "symbols_pick_add": {"en": "➕ Tap a symbol to ADD it:", "fa": "➕ روی نمادی که می‌خوای اضافه کنی کلیک کن:"},
    "symbols_pick_remove": {"en": "➖ Tap a symbol to REMOVE it:", "fa": "➖ روی نمادی که می‌خوای حذف کنی کلیک کن:"},
    "symbols_added": {"en": "✅ Added {symbol}", "fa": "✅ {symbol} اضافه شد"},
    "symbols_removed": {"en": "✅ Removed {symbol}", "fa": "✅ {symbol} حذف شد"},
    "symbols_already": {"en": "Already active: {symbol}", "fa": "قبلاً فعاله: {symbol}"},
    "symbols_not_active": {"en": "Not active: {symbol}", "fa": "فعال نیست: {symbol}"},
    "closeall_queued": {"en": "🔒 Close-all queued — core engine will execute it within one cycle.", "fa": "🔒 درخواست بسته‌شدن همه‌ی پوزیشن‌ها ثبت شد — موتور اصلی در چرخه‌ی بعدی اجراش می‌کنه."},
    "cancelled": {"en": "Cancelled.", "fa": "لغو شد."},
    "strategy_set": {"en": "🧠 Active strategy → {label}\n{desc}",
                      "fa": "🧠 استراتژی فعال → {label}\n{desc}"},
    "symbols_usage": {"en": ("Usage:\n/symbols — show current\n/symbols set ETH,BTC,SOL\n/symbols add BTC\n/symbols remove BTC"),
                       "fa": ("استفاده:\n/symbols — نمایش نمادهای فعلی\n/symbols set ETH,BTC,SOL\n/symbols add BTC\n/symbols remove BTC")},
    "symbols_current": {"en": "🎯 Trading symbols: {symbols}", "fa": "🎯 نمادهای فعال: {symbols}"},
    "timeframes_usage": {"en": "Usage: /timeframes set 15m,1h,4h  (first = entry timing timeframe)",
                          "fa": "استفاده: /timeframes set 15m,1h,4h  (اولین مورد = تایم‌فریم ورود)"},
    "timeframes_current": {"en": "⏱ Timeframes: {timeframes}", "fa": "⏱ تایم‌فریم‌های فعال: {timeframes}"},
    "report_every_usage": {"en": "Usage: /reportevery <hours>  (0 disables auto reports)",
                            "fa": "استفاده: /reportevery <ساعت>  (۰ = غیرفعال کردن گزارش خودکار)"},
    "report_every_set": {"en": "⏰ Auto P&L report interval set to {h}h (0 = off)",
                          "fa": "⏰ فاصله‌ی گزارش خودکار سود/زیان: {h} ساعت (۰ = خاموش)"},
    "status": {
        "en": (
            "📋 <b>STATUS</b> | {network}\n"
            "━━━━━━━━━━━━\n"
            "💰 Total balance: ${capital}\n"
            "🪙 Spot: ${spot}  |  📊 Perp: ${perp}\n"
            "⚙️ Max capital (cap): ${equity}\n"
            "🔓 Open positions: {open_count}\n"
            "⏳ Today PnL: {today_pnl}\n"
            "🤖 AI: {model}\n"
            "🧠 Strategy: {strategy}\n"
            "🎯 Symbols: {symbols}\n"
            "⚙️ Lev.cap: {max_leverage}x | Risk/trade: {risk_pct}%\n"
            "🚨 Daily loss limit: {max_daily_loss_pct}% | Max losing streak: {max_consecutive_losses}\n"
            "🔘 Paused: {paused}\n"
            "━━━━━━━━━━━━"
        ),
        "fa": (
            "📋 <b>وضعیت</b> | {network}\n"
            "━━━━━━━━━━━━\n"
            "💰 موجودی کل: {capital}$\n"
            "🪙 اسپات: {spot}$  |  📊 پرپ: {perp}$\n"
            "⚙️ سقف کل سرمایه: ${equity}\n"
            "🔓 پوزیشن‌های باز: {open_count}\n"
            "⏳ سود/زیان امروز: {today_pnl}\n"
            "🤖 هوش مصنوعی: {model}\n"
            "🧠 استراتژی: {strategy}\n"
            "🎯 نمادها: {symbols}\n"
            "⚙️ سقف لوریج: {max_leverage}x | ریسک هر معامله: {risk_pct}٪\n"
            "🚨 سقف ضرر روزانه: {max_daily_loss_pct}٪ | سقف باخت متوالی: {max_consecutive_losses}\n"
            "🔘 متوقف: {paused}\n"
            "━━━━━━━━━━━━"
        ),
    },
    "settings_dump": {
        "en": (
            "🗂 <b>ALL SETTINGS</b>\n"
            "━━━━━━━━━━━━\n"
            "💵 Capital ceiling: ${capital_usd} (start: ${starting_capital_usd})\n"
            "📈 Max leverage: {max_leverage}x\n"
            "🎯 Risk/trade: {risk_per_trade_pct}%\n"
            "🚨 Max daily loss: {max_daily_loss_pct}% | Max losing streak: {max_consecutive_losses}\n"
            "💲 Min order size: ${min_notional_usd}\n"
            "📦 Sizing mode: {sizing_mode} | Trade %: {trade_capital_pct}%\n"
            "🔑 AI keys configured: {ai_keys}\n"
            "🧠 Strategy: {strategy}\n"
            "🎯 Symbols: {symbols}\n"
            "⏱ Timeframes: {timeframes}\n"
            "⏰ Auto report every: {report_interval_hours}h\n"
            "🌐 Language: {language}\n"
            "🔘 Paused: {paused}\n"
            "━━━━━━━━━━━━"
        ),
        "fa": (
            "🗂 <b>همه‌ی تنظیمات</b>\n"
            "━━━━━━━━━━━━\n"
            "💵 سقف سرمایه: {capital_usd}$ (سرمایه‌ی اولیه: {starting_capital_usd}$)\n"
            "📈 سقف لوریج: {max_leverage}x\n"
            "🎯 ریسک هر معامله: {risk_per_trade_pct}٪\n"
            "🚨 سقف ضرر روزانه: {max_daily_loss_pct}٪ | سقف باخت متوالی: {max_consecutive_losses}\n"
            "💲 حداقل حجم سفارش: {min_notional_usd}$\n"
            "📦 حالت حجم معامله: {sizing_mode} | درصد معامله: {trade_capital_pct}٪\n"
            "🔑 کلیدهای هوش تنظیم‌شده: {ai_keys}\n"
            "🧠 استراتژی: {strategy}\n"
            "🎯 نمادها: {symbols}\n"
            "⏱ تایم‌فریم‌ها: {timeframes}\n"
            "⏰ فاصله گزارش خودکار: {report_interval_hours} ساعت\n"
            "🌐 زبان: {language}\n"
            "🔘 متوقف: {paused}\n"
            "━━━━━━━━━━━━"
        ),
    },
    "pnl_report": {
        "en": (
            "💹 <b>P&L REPORT — {period}</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            "📈 Closed trades: {count}  (✅ {wins} win / ❌ {losses} loss)\n"
            "🏆 Win rate: {win_rate}%\n"
            "💰 Net PnL: {total_pnl} USD ({pnl_pct}% of starting capital)\n"
            "📊 Avg win: {avg_win} | Avg loss: {avg_loss}\n"
            "🥇 Best: {best_symbol} ({best_pnl})\n"
            "🥴 Worst: {worst_symbol} ({worst_pnl})\n"
            "━━━━━━━━━━━━━━━━━\n"
            "🕐 {timestamp}"
        ),
        "fa": (
            "💹 <b>گزارش سود/زیان — {period}</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            "📈 معاملات بسته‌شده: {count}  (✅ {wins} برد / ❌ {losses} باخت)\n"
            "🏆 نرخ برد: {win_rate}٪\n"
            "💰 سود/زیان خالص: {total_pnl} دلار ({pnl_pct}٪ از سرمایه‌ی اولیه)\n"
            "📊 میانگین سود: {avg_win} | میانگین ضرر: {avg_loss}\n"
            "🥇 بهترین: {best_symbol} ({best_pnl})\n"
            "🥴 بدترین: {worst_symbol} ({worst_pnl})\n"
            "━━━━━━━━━━━━━━━━━\n"
            "🕐 {timestamp}"
        ),
    },
    "pnl_usage": {"en": "Usage: /pnl daily|weekly|monthly", "fa": "استفاده: /pnl daily|weekly|monthly"},
    "help": {
        "en": (
            "<b>📋 Status &amp; reports</b>\n"
            "/status — full status\n"
            "/positions — open positions\n"
            "/trades [n] — last n trades\n"
            "/logs [n] — last n logs\n"
            "/pnl daily|weekly|monthly — P&amp;L report\n"
            "/reportevery &lt;hours&gt; — auto report interval (0=off)\n"
            "/settings — dump every current setting\n"
            "/balance — wallet balance\n"
            "\n<b>⚙️ Risk &amp; capital</b>\n"
            "/setcapital &lt;n&gt; /setstartcapital &lt;n&gt; /setleverage &lt;n&gt; /setrisk &lt;n&gt;\n"
            "/setdailyloss &lt;pct&gt; /setconsecutivelosses &lt;n&gt;\n"
            "/sizingmode auto|fixed  /settradepct &lt;1-100&gt;\n"
            "\n<b>🤖 AI</b>\n"
            "/models — view the fixed 4-stage AI pipeline (chart→fundamental→synthesis→decision)\n"
            "/setapikey — add/replace an AI provider key (OpenRouter/OpenAI/Gemini), or\n"
            "/setapikey &lt;provider&gt; &lt;key&gt; to set one directly\n"
            "/strategy — pick analysis persona (with descriptions)\n"
            "\n<b>🎯 Market</b>\n"
            "/symbols — view/set/add/remove traded symbols\n"
            "/timeframes set 15m,1h,4h — set analysis timeframes\n"
            "\n<b>🕹 Control</b>\n"
            "/pause /resume /closeall /restart\n"
            "/killswitch — kill-switch state\n"
            "/lang en|fa\n"
            "/help — this list"
        ),
        "fa": (
            "<b>📋 وضعیت و گزارش</b>\n"
            "/status — وضعیت کامل\n"
            "/positions — پوزیشن‌های باز\n"
            "/trades [n] — آخرین n معامله\n"
            "/logs [n] — آخرین n لاگ\n"
            "/pnl daily|weekly|monthly — گزارش سود/زیان\n"
            "/reportevery &lt;ساعت&gt; — فاصله‌ی گزارش خودکار (۰=خاموش)\n"
            "/settings — نمایش همه‌ی تنظیمات فعلی\n"
            "/balance — موجودی کیف پول\n"
            "\n<b>⚙️ ریسک و سرمایه</b>\n"
            "/setcapital &lt;n&gt; /setstartcapital &lt;n&gt; /setleverage &lt;n&gt; /setrisk &lt;n&gt;\n"
            "/setdailyloss &lt;درصد&gt; /setconsecutivelosses &lt;n&gt;\n"
            "/sizingmode auto|fixed  /settradepct &lt;۱-۱۰۰&gt;\n"
            "\n<b>🤖 هوش مصنوعی</b>\n"
            "/models — نمایش پایپ‌لاین ثابت ۴ مرحله‌ای (چارت→فاندامنتال→ترکیب→تصمیم)\n"
            "/setapikey — افزودن/تعویض کلید یک پروایدر (OpenRouter/OpenAI/Gemini)، یا\n"
            "/setapikey &lt;پروایدر&gt; &lt;کلید&gt; برای تنظیم مستقیم\n"
            "/strategy — انتخاب استراتژی تحلیل (همراه توضیح)\n"
            "\n<b>🎯 بازار</b>\n"
            "/symbols — نمایش/تنظیم/افزودن/حذف نمادهای معاملاتی\n"
            "/timeframes set 15m,1h,4h — تنظیم تایم‌فریم‌های تحلیل\n"
            "\n<b>🕹 کنترل</b>\n"
            "/pause /resume /closeall /restart\n"
            "/killswitch — وضعیت توقف اضطراری\n"
            "/lang en|fa\n"
            "/help — همین لیست"
        ),
    },
}


def t(key: str, lang: str, **kwargs) -> str:
    lang = lang if lang in ("en", "fa") else "en"
    template = STRINGS.get(key, {}).get(lang, key)
    try:
        return template.format(**kwargs)
    except Exception:
        return template
