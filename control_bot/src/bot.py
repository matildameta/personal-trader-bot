"""
Separate Telegram bot (own token) that gives you a menu + full slash
commands to change live settings and view reports. It never holds
exchange keys — it only reads/writes the shared SQLite DB that
core_engine also reads every cycle, and queues "closeall"/"restart_engine"
as pending commands since those need real exchange access (or need to
happen inside the engine's own process).
Restricted to a single allowed_chat_id.
"""
import logging
import os
import sys
import time
import asyncio
from datetime import datetime, timezone

import requests
import yaml
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)

from .shared_db import ControlDB
from .i18n import t
from .catalog import PIPELINE_STAGES, LLM_PROVIDERS, STRATEGIES, OR_PERSONAS, LOOP_INTERVALS
from .hl_pnl import pnl_for_period
from .comprehensive_report import build_comprehensive

# Popular perps on Hyperliquid (used for the add-symbol button grid).
# These are the most-traded coins; full list is fetched live from the
# exchange when building the add picker.
POPULAR_SYMBOLS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX",
    "LINK", "SUI", "TON", "TRX", "ATOM", "DOT", "MATIC", "NEAR",
    "APT", "ARB", "OP", "INJ", "TIA", "SEI", "WIF", "PEPE",
    "LTC", "UNI", "AAVE", "MKR", "RNDR", "FIL",
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("control_bot")

MAX_SAFE_LEVERAGE = 10
INT_KEYS = {"max_leverage", "max_consecutive_losses"}
PENDING_EDIT = {}   # chat_id -> settings key awaiting a typed value
PENDING_EDIT_PROMPT = {}   # chat_id -> message_id of the "enter new value" prompt (to delete on reply)
PENDING_EDIT_MENU_MSG = {}   # chat_id -> message_id of the menu prompt being edited

# Engine config lives one level up in core_engine/. The control bot runs from
# the control_bot/ directory, so this relative path resolves correctly.
ENGINE_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "core_engine", "config.yaml")

def _load_engine_config():
    import yaml as _yaml
    path = os.path.abspath(ENGINE_CONFIG_PATH)
    with open(path) as f:
        return _yaml.safe_load(f), path


def _save_engine_config(cfg, path):
    import yaml as _yaml
    with open(path, "w") as f:
        _yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _save_api_key(secret_key: str, account_address: str, network: str = "testnet"):
    """Save the Hyperliquid wallet key + derived address into engine config for the given network."""
    cfg, path = _load_engine_config()
    cfg.setdefault("hyperliquid", {})
    cfg["hyperliquid"].setdefault(network, {})
    cfg["hyperliquid"][network]["secret_key"] = secret_key
    cfg["hyperliquid"][network]["account_address"] = account_address
    _save_engine_config(cfg, path)


def _save_llm_key(provider: str, api_key: str):
    """Save/replace ONE AI provider's key into its own slot in engine config
    (llm.api_keys.<provider>) -- every other provider's key already saved
    there is left untouched, so adding OpenAI after OpenRouter (or vice
    versa) never wipes the other one out. The fixed 4-stage pipeline in
    ai_pipeline.py picks whichever provider each stage needs at call time."""
    cfg, path = _load_engine_config()
    cfg.setdefault("llm", {})
    cfg["llm"].setdefault("api_keys", {})
    cfg["llm"]["api_keys"][provider] = api_key
    _save_engine_config(cfg, path)


def _configured_llm_providers() -> set:
    cfg, _ = _load_engine_config()
    keys = (cfg.get("llm", {}) or {}).get("api_keys", {}) or {}
    return {p for p, v in keys.items() if v}


def _test_llm_token(provider: str, api_key: str) -> tuple[bool, str]:
    """Ping a provider with a tiny request. Returns (ok, detail)."""
    try:
        if provider in ("openrouter", "openrouter_backup", "godmode_openrouter"):
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": "deepseek/deepseek-chat", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
                timeout=20,
            )
            if r.status_code == 200:
                return True, "OK"
            return False, f"HTTP {r.status_code}: {r.text[:120]}"
        elif provider == "openai":
            r = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
                timeout=20,
            )
            if r.status_code == 200:
                return True, "OK"
            return False, f"HTTP {r.status_code}: {r.text[:120]}"
        elif provider in ("gemini", "godmode_gemini"):
            r = requests.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
                timeout=20,
            )
            if r.status_code == 200:
                return True, "OK"
            return False, f"HTTP {r.status_code}: {r.text[:120]}"
        elif provider in ("godmode_nvidia", "nvidia"):
            # Test all models used by the nvidia strategy
            models_to_test = [
                "nvidia/nemotron-3-ultra-550b-a55b",      # Chart stage
                "meta/llama-3.1-70b-instruct",            # Fundamental, Synthesis, Decision (and GOD MODE NVIDIA)
            ]
            results = []
            for model in models_to_test:
                r = requests.post(
                    "https://integrate.api.nvidia.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
                    timeout=60,
                )
                if r.status_code == 200:
                    results.append(f"{model}: OK")
                else:
                    results.append(f"{model}: HTTP {r.status_code} {r.text[:80]}")
            all_ok = all("OK" in res for res in results)
            return all_ok, " | ".join(results)
    except Exception as e:
        return False, f"error: {e}"
    return False, "unknown provider"


def _test_all_tokens_text(lang: str) -> str:
    cfg, _ = _load_engine_config()
    keys = (cfg.get("llm", {}) or {}).get("api_keys", {}) or {}
    lines = []
    for prov in ("openrouter", "openrouter_backup", "openai", "gemini",
                 "godmode_openrouter", "godmode_gemini", "godmode_nvidia", "nvidia"):
        key = keys.get(prov, "")
        if not key:
            mark = "⚪️" if lang == "fa" else "⚪️"
            label = "not set"
            lines.append(f"{mark} {prov}: {label}")
            continue
        ok, detail = _test_llm_token(prov, key)
        if ok:
            mark = "🟢" if lang == "fa" else "🟢"
            lines.append(f"{mark} {prov}: OK")
        else:
            mark = "🔴" if lang == "fa" else "🔴"
            # short detail
            detail = detail[:80]
            lines.append(f"{mark} {prov}: FAIL ({detail})")
    title = "🔍 نتیجه تست توکن‌ها:" if lang == "fa" else "🔍 AI token test results:"
    return title + "\n\n" + "\n".join(lines)


def _test_models_text(lang: str) -> str:
    """Run a real sample analysis through every strategy's pipeline and
    report the resulting signal/confidence so the user can confirm the
    models actually respond (not just that the key is 'OK'). Delegates to
    core_engine/src/model_probe.py (run as a subprocess with `python -m`
    so ai_pipeline's relative imports resolve correctly)."""
    import subprocess
    core_src = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "core_engine", "src"))
    core_root = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "core_engine"))
    probe = os.path.join(core_src, "model_probe.py")
    venv_py = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", ".venv", "bin", "python"))
    if not os.path.exists(venv_py):
        venv_py = sys.executable
    try:
        out = subprocess.run(
            [venv_py, "-m", "src.model_probe", lang],
            cwd=core_root,
            capture_output=True, text=True, timeout=150,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
        err = out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "unknown error"
        return ("❌ تست مدل‌ها شکست خورد: " if lang == "fa" else f"❌ Model test failed: {err}")
    except Exception as e:
        return ("❌ نمی‌توانم تست مدل‌ها را اجرا کنم: " if lang == "fa"
                else f"❌ Cannot run model test: {e}")

COMMANDS_EN = [
    ("start", "Open the control panel"),
    ("status", "Full bot status"),
    ("positions", "Open positions"),
    ("balance", "Wallet balance"),
    ("trades", "Recent trades"),
    ("pnl", "P&L report: daily/weekly/monthly"),
    ("reportevery", "Auto report interval (hours)"),
    ("settings", "Show every current setting"),
    ("logs", "Recent engine logs"),
    ("models", "View the fixed AI pipeline"),
    ("setapikey", "Add/replace an AI provider key (OpenRouter/OpenAI/Gemini)"),
    ("strategy", "Pick analysis strategy"),
    ("symbols", "View/set traded symbols"),
    ("timeframes", "Set analysis timeframes"),
    ("setcapital", "Set capital ceiling ($)"),
    ("setstartcapital", "Set starting capital baseline ($)"),
    ("setleverage", "Set max leverage"),
    ("setrisk", "Set risk % per trade"),
    ("setdailyloss", "Set max daily loss %"),
    ("setconsecutivelosses", "Set max consecutive losses"),
    ("sizingmode", "auto (AI) or fixed sizing"),
    ("settradepct", "Trade size % of capital"),
    ("transfer", "Move funds spot<->perp"),
    ("pause", "Pause new trades"),
    ("resume", "Resume trading"),
    ("closeall", "Close ALL open positions"),
    ("killswitch", "Show kill-switch state"),
    ("restart", "Restart bot + engine"),
    ("lang", "Switch language en|fa"),
    ("help", "Full command list"),
]
COMMANDS_FA = [
    ("start", "باز کردن پنل کنترل"),
    ("status", "وضعیت کامل بات"),
    ("positions", "پوزیشن‌های باز"),
    ("balance", "موجودی کیف پول"),
    ("trades", "معاملات اخیر"),
    ("pnl", "گزارش سود/زیان روزانه/هفتگی/ماهانه"),
    ("reportevery", "فاصله‌ی گزارش خودکار (ساعت)"),
    ("settings", "نمایش همه‌ی تنظیمات"),
    ("logs", "لاگ‌های اخیر موتور"),
    ("models", "نمایش پایپ‌لاین ثابت هوش مصنوعی"),
    ("setapikey", "افزودن/تعویض کلید یک پروایدر هوش (OpenRouter/OpenAI/Gemini)"),
    ("strategy", "انتخاب استراتژی تحلیل"),
    ("symbols", "نمایش/تنظیم نمادهای معاملاتی"),
    ("timeframes", "تنظیم تایم‌فریم‌های تحلیل"),
    ("setcapital", "تنظیم سقف سرمایه ($)"),
    ("setstartcapital", "تنظیم سرمایه‌ی پایه ($)"),
    ("setleverage", "تنظیم سقف لوریج"),
    ("setrisk", "تنظیم درصد ریسک هر معامله"),
    ("setdailyloss", "تنظیم سقف ضرر روزانه (٪)"),
    ("setconsecutivelosses", "تنظیم سقف باخت متوالی"),
    ("sizingmode", "حالت حجم معامله: auto یا fixed"),
    ("settradepct", "درصد سرمایه در هر معامله"),
    ("transfer", "انتقال بین اسپات و پرپ"),
    ("pause", "توقف معاملات جدید"),
    ("resume", "ازسرگیری معاملات"),
    ("closeall", "بستن همه‌ی پوزیشن‌ها"),
    ("killswitch", "وضعیت توقف اضطراری"),
    ("restart", "ری‌استارت بات و موتور"),
    ("lang", "تغییر زبان en|fa"),
    ("help", "لیست کامل دستورات"),
]


def authorized(update: Update, allowed_chat_id: str) -> bool:
    return str(update.effective_chat.id) == str(allowed_chat_id)


def _lang(db: ControlDB) -> str:
    return db.get_settings().get("language", "en")


def menu_with_ok(lang: str, text: str, parse_mode=None) -> tuple:
    """Build a reply with the content text plus a single ✅ OK button that
    returns to the main menu."""
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(t("ok", lang), callback_data="main_menu")
    ]])
    return text, kb


async def refresh_to_menu(query, db: ControlDB, lang: str):
    """Replace the current temporary message IN PLACE with the main menu
    (edit, not delete+new) so we never stack multiple menu messages."""
    try:
        await query.edit_message_text(t("menu_title", lang), reply_markup=build_menu(db, lang))
    except Exception:
        pass


async def risk_mode_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show risk mode chooser: AUTO (rule_engine + drawdown throttle + vol leverage)
    or MANUAL (user sets base risk_pct, throttle can only reduce it)."""
    q = update.callback_query
    await q.answer()
    db: ControlDB = context.bot_data["db"]
    lang = _lang(db)
    s = db.get_settings()
    mode = s.get("risk_mode", "manual")
    rows = [
        [InlineKeyboardButton(
            ("✅ " if mode == "auto" else "⚙️ ") + ("اتومات (هوشمند)" if lang == "fa" else "AUTO (smart)"),
            callback_data="risk_mode:auto")],
        [InlineKeyboardButton(
            ("✅ " if mode == "manual" else "✍️ ") + ("دستی (عدد ثابت)" if lang == "fa" else "MANUAL (fixed)"),
            callback_data="risk_mode:manual")],
        [InlineKeyboardButton("↩️ " + ("بازگشت" if lang == "fa" else "Back"), callback_data="main_menu")],
    ]
    txt = ("🎚 <b>حالت ریسک معامله</b>\n\n"
           "🔹 <b>اتومات</b>: ریسک رو سیستم خودش تنظیم می‌کنه — روزهای ضرر کمتر، "
           "بازار وحشی لوریج کمتر، و طبق استراتژی. بهترین حالت.\n"
           "🔹 <b>دستی</b>: خودت عدد رو می‌ذاری (سقف پایه)، سیستم فقط ازش کم‌تر نمی‌کنه.\n\n"
           f"حالت فعلی: <b>{'اتومات' if mode=='auto' else 'دستی (' + str(s.get('risk_per_trade_pct')) + '٪)'}</b>")
    await q.edit_message_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def risk_mode_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Apply chosen risk mode. AUTO lets the engine manage risk; MANUAL keeps risk_per_trade_pct as base."""
    q = update.callback_query
    await q.answer()
    db: ControlDB = context.bot_data["db"]
    lang = _lang(db)
    mode = q.data.split(":", 1)[1]
    db.set_setting("risk_mode", mode)
    if mode == "auto":
        # transient confirmation: edit in place, then refresh menu after a few seconds
        try:
            await q.edit_message_text(
                "✅ <b>ریسک اتومات تنظیم شد</b>\n\nسیستم ریسک هر معامله رو طبق استراتژی و وضعیت بازار "
                "خودش تنظیم می‌کند (روزهای ضرر → کمتر، بازار وحشی → لوریج کمتر). پیام خودکار پاک می‌شود.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ " + ("بازگشت" if lang == "fa" else "Back"), callback_data="main_menu")]]),
            )
            await asyncio.sleep(3)
        except Exception:
            pass
    # rebuild the main menu (with updated risk button label)
    await refresh_to_menu(q, db, lang)


async def transfer_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show transfer submenu: spot->perp, perp->spot, or full balance."""
    q = update.callback_query
    await q.answer()
    db: ControlDB = context.bot_data["db"]
    lang = _lang(db)
    s = db.get_settings()
    try:
        from hyperliquid_client import HyperliquidClient
    except Exception:
        from hyperliquid_client import HyperliquidClient
    # build a temporary client to show current balances
    try:
        hl = HyperliquidClient(
            account_address=cfg_secret_address(context),
            secret_key=cfg_secret_key(context),
            network=s.get("network", "testnet"),
        )
        spot = hl.get_spot_usdc()
        perp = hl.get_perp_usdc()
    except Exception as e:
        spot = perp = 0.0
    rows = [
        [InlineKeyboardButton("🟢 اسپات → فیوچر" if lang == "fa" else "🟢 SPOT → PERP", callback_data="transfer:perp")],
        [InlineKeyboardButton("🟣 فیوچر → اسپات" if lang == "fa" else "🟣 PERP → SPOT", callback_data="transfer:spot")],
        [InlineKeyboardButton("💎 کل موجودی → فیوچر" if lang == "fa" else "💎 ALL → PERP", callback_data="transfer:all_perp")],
        [InlineKeyboardButton("💎 کل موجودی → اسپات" if lang == "fa" else "💎 ALL → SPOT", callback_data="transfer:all_spot")],
        [InlineKeyboardButton("↩️ " + ("بازگشت" if lang == "fa" else "Back"), callback_data="main_menu")],
    ]
    txt = (f"🔄 <b>انتقالات اسپات ⇄ فیوچر</b>\n\n"
           f"💵 اسپات (SPOT): <b>{spot:.2f}</b> USDC\n"
           f"⚡ فیوچر (PERP): <b>{perp:.2f}</b> USDC\n\n"
           f"جهت را انتخاب کن؛ سپس مقدار را به صورت پیام بنویس.")
    await q.edit_message_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


def cfg_secret_key(context):
    """Read the engine's secret key from its config.yaml (control bot shares DB but not keys)."""
    import yaml, os
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "core_engine", "config.yaml")
    try:
        with open(p) as f:
            cfg = yaml.safe_load(f)
        net = cfg.get("network", "testnet")
        return cfg.get("hyperliquid", {}).get(net, {}).get("secret_key", "")
    except Exception:
        return ""


def cfg_secret_address(context):
    import yaml, os
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "core_engine", "config.yaml")
    try:
        with open(p) as f:
            cfg = yaml.safe_load(f)
        net = cfg.get("network", "testnet")
        return cfg.get("hyperliquid", {}).get(net, {}).get("account_address", "")
    except Exception:
        return ""


async def transfer_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Perform the transfer after the user typed an amount (or full balance)."""
    q = update.callback_query
    await q.answer()
    db: ControlDB = context.bot_data["db"]
    lang = _lang(db)
    mode = q.data.split(":", 1)[1]  # perp | spot | all_perp | all_spot
    s = db.get_settings()
    # resolve amount + direction
    if mode == "perp":
        direction, to_perp = "SPOT→PERP", True
        # need amount from pending
        PENDING_TRANSFER[update.effective_chat.id] = {"mode": mode, "to_perp": True, "full": False}
        await q.edit_message_text("💵 مقدار USDC برای انتقال به فیوچر را بنویس:" if lang == "fa" else "💵 Type the USDC amount to move to PERP:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ " + ("بازگشت" if lang == "fa" else "Back"), callback_data="transfer_menu")]]))
        return
    if mode == "spot":
        direction, to_perp = "PERP→SPOT", False
        PENDING_TRANSFER[update.effective_chat.id] = {"mode": mode, "to_perp": False, "full": False}
        await q.edit_message_text("💵 مقدار USDC برای انتقال به اسپات را بنویس:" if lang == "fa" else "💵 Type the USDC amount to move to SPOT:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ " + ("بازگشت" if lang == "fa" else "Back"), callback_data="transfer_menu")]]))
        return
    if mode in ("all_perp", "all_spot"):
        to_perp = (mode == "all_perp")
        direction = "SPOT→PERP" if to_perp else "PERP→SPOT"
        # do full transfer immediately
        try:
            from hyperliquid_client import HyperliquidClient
        except Exception:
            from hyperliquid_client import HyperliquidClient
        _secret = cfg_secret_key(context)
        if not _secret or len(_secret) < 10:
            await q.edit_message_text(
                "⚠️ کلید خصوصی کیف‌پول ست نشده است.\nابتدا از بخش تنظیمات، کلید خود را وارد کنید." if lang == "fa" else
                "⚠️ Wallet private key is not set.\nPlease set your key in settings first.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ " + ("بازگشت" if lang == "fa" else "Back"), callback_data="transfer_menu")]]))
            return
        hl = HyperliquidClient(account_address=cfg_secret_address(context), secret_key=_secret, network=s.get("network", "testnet"))
        if to_perp:
            amt = hl.get_spot_usdc()
        else:
            amt = hl.get_perp_usdc()
        if amt <= 0:
            await q.edit_message_text("⚠️ موجودی منبع خالی است." if lang == "fa" else "⚠️ Source wallet is empty.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ " + ("بازگشت" if lang == "fa" else "Back"), callback_data="transfer_menu")]]))
            return
        r = hl.transfer_between_wallets(amt, to_perp)
        if r.get("ok"):
            txt = (f"✅ <b>{r['msg']}</b>\n\n"
                   f"SPOT: {r['spot_after']:.2f} | PERP: {r['perp_after']:.2f} USDC")
        else:
            txt = f"❌ {r.get('msg','خطا')}"
        await q.edit_message_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ " + ("بازگشت" if lang == "fa" else "Back"), callback_data="transfer_menu")]]))
        return


PENDING_TRANSFER = {}


async def adv_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Advanced settings: per-symbol daily trade cap + Claude recommended defaults."""
    q = update.callback_query
    await q.answer()
    db: ControlDB = context.bot_data["db"]
    lang = _lang(db)
    s = db.get_settings()
    cap = s.get("max_trades_per_symbol_per_day", 3)
    cap_txt = cap if cap else "—"
    rows = [
        [InlineKeyboardButton(
            ("🔢 سقف معاملات روزانه: " + str(cap_txt)) if lang == "fa" else ("🔢 Daily trade cap: " + str(cap_txt)),
            callback_data="adv_set_cap")],
        [InlineKeyboardButton(
            "✅ اعمال پیش‌فرض" if lang == "fa" else "✅ Apply recommended defaults",
            callback_data="adv_apply_claude")],
        [InlineKeyboardButton("↩️ " + ("بازگشت" if lang == "fa" else "Back"), callback_data="main_menu")],
    ]
    txt = ("⚙️ <b>تنظیمات پیشرفته</b>\n\n"
           "🔢 <b>سقف معاملات روزانه</b>: حداکثر تعداد معامله‌ای که روی هر نماد (مثل ETH) در هر روز باز میشه.\n"
           "این فقط یه <b>سقف مجاز</b> هست — تایید نهایی هنوز با فیلترهای هوشمند (rule_engine) است. "
           "عدد بالاتر = معاملات بیشتر + کارمزد بیشتر.\n\n"
           "✅ <b>پیش‌فرض</b>: تنظیمات پیشنهادی را اعمال می‌کند:\n"
           "• سقف روزانه هر نماد: ۳\n"
           "• کول‌داون بین معاملات: ۱۵ دقیقه\n"
           "• تایید برعکس‌کردن: ۲ چرخه\n"
           "• حداقل ATR برای استاپ: ۱.۲×")
    await q.edit_message_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def adv_apply_claude(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Apply Claude's recommended defaults for the trade-gate tunables."""
    q = update.callback_query
    await q.answer()
    db: ControlDB = context.bot_data["db"]
    lang = _lang(db)
    db.set_setting("max_trades_per_symbol_per_day", 3)
    db.set_setting("cooldown_seconds", 900)
    db.set_setting("reversal_confirm_cycles", 2)
    db.set_setting("atr_sl_min_multiple", 1.2)
    await q.edit_message_text(
        "✅ <b>پیش‌فرض اعمال شد</b>\n\n"
        "• سقف روزانه هر نماد: ۳\n• کول‌داون: ۱۵ دقیقه\n• تایید برعکس: ۲ چرخه\n• حداقل ATR استاپ: ۱.۲×",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ " + ("بازگشت" if lang == "fa" else "Back"), callback_data="adv_menu")]]),
    )


PENDING_ADV_CAP = {}


def build_menu(db: ControlDB, lang: str) -> InlineKeyboardMarkup:
    s = db.get_settings()
    current_network = s.get("network", "testnet")
    network_label = "🧪 Testnet" if current_network == "testnet" else "🔴 Mainnet"
    rows = [
        [InlineKeyboardButton(t("btn_status", lang), callback_data="status"),
         InlineKeyboardButton(t("btn_capital", lang, v=s.get("capital_usd")), callback_data="edit:capital_usd")],
        [InlineKeyboardButton(t("btn_leverage", lang, v=s.get("max_leverage")), callback_data="edit:max_leverage"),
         InlineKeyboardButton("🎚 ریسک: " + ("اتومات" if s.get("risk_mode") == "auto" else f"{s.get('risk_per_trade_pct')}٪"), callback_data="risk_mode_menu")],
        [InlineKeyboardButton(t("btn_daily_loss", lang, v=s.get("max_daily_loss_pct")), callback_data="edit:max_daily_loss_pct"),
         InlineKeyboardButton(t("btn_consec_losses", lang, v=s.get("max_consecutive_losses")), callback_data="edit:max_consecutive_losses")],
        [InlineKeyboardButton(t("btn_language", lang, v=lang), callback_data="toggle_lang")],
        [InlineKeyboardButton("🌐 " + network_label, callback_data="toggle_network")],
        [InlineKeyboardButton(t("btn_ai_test", lang), callback_data="test_tokens"),
         InlineKeyboardButton("⚡ " + ("تست سریع" if lang == "fa" else "Quick test"), callback_data="test_quick")],
        [InlineKeyboardButton(t("btn_strategy", lang), callback_data="strategy")],
        [InlineKeyboardButton("⏱ " + ("فاصله بررسی" if lang == "fa" else "Poll interval"), callback_data="loop_interval")],
        [InlineKeyboardButton(t("btn_symbols", lang), callback_data="symbols"),
         InlineKeyboardButton(t("btn_settings", lang), callback_data="settings")],
        [InlineKeyboardButton("💰 " + ("موجودی (هایپرلیکوئید)" if lang == "fa" else "Balance (Hyperliquid)"), callback_data="balance_hl")],
        [InlineKeyboardButton("📊 " + ("گزارش جامع" if lang == "fa" else "Full report"), callback_data="full_report")],
        [InlineKeyboardButton("🔔 " + ("تنظیمات گزارش‌ها و هشدارها" if lang == "fa" else "Report & alert settings"), callback_data="reports_menu")],
        [InlineKeyboardButton(
            t("btn_resume", lang) if s.get("paused") else t("btn_pause", lang),
            callback_data="ask_resume" if s.get("paused") else "toggle_pause",
        )],
        [InlineKeyboardButton(t("btn_pnl", lang), callback_data="pnl_menu")],
        [InlineKeyboardButton(t("btn_report", lang), callback_data="report"),
         InlineKeyboardButton(t("btn_logs", lang), callback_data="logs")],
        [InlineKeyboardButton(t("btn_closeall", lang), callback_data="ask_closeall"),
         InlineKeyboardButton(t("btn_restart", lang), callback_data="ask_restart")],
        [InlineKeyboardButton(t("btn_restart_ctl", lang), callback_data="ask_restart_ctl")],
        [InlineKeyboardButton(t("btn_ai_keys", lang), callback_data="apikeys")],
        [InlineKeyboardButton(t("btn_set_apikey", lang), callback_data="ask_setkey")],
        [InlineKeyboardButton("🔄 انتقالات (اسپات⇄فیوچر)" if lang == "fa" else "🔄 Transfers (spot⇄perp)", callback_data="transfer_menu")],
        [InlineKeyboardButton("⚙️ تنظیمات پیشرفته" if lang == "fa" else "⚙️ Advanced settings", callback_data="adv_menu")],
    ]
    return InlineKeyboardMarkup(rows)


def _symbols_menu_text(db: ControlDB, lang: str) -> str:
    s = db.get_settings()
    active = s.get("symbols", [])
    choices = ", ".join(active) if active else "—"
    return t("symbols_active", lang, choices=choices)


def symbols_menu_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t("btn_add_symbol", lang), callback_data="sym_add"),
         InlineKeyboardButton(t("btn_remove_symbol", lang), callback_data="sym_remove")],
        [InlineKeyboardButton(t("btn_back", lang), callback_data="main_menu")],
    ])


def _symbol_picker_keyboard(db: ControlDB, lang: str, mode: str) -> InlineKeyboardMarkup:
    """mode = 'add' (show popular symbols to add) or 'remove' (show active)."""
    s = db.get_settings()
    active = set(x.upper() for x in s.get("symbols", []))
    if mode == "add":
        candidates = [x for x in POPULAR_SYMBOLS if x not in active] or POPULAR_SYMBOLS
    else:
        candidates = list(active) or ["ETH"]

    rows = []
    row = []
    for sym in candidates:
        cb = f"sym_add:{sym}" if mode == "add" else f"sym_remove:{sym}"
        row.append(InlineKeyboardButton(sym, callback_data=cb))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(t("btn_back", lang), callback_data="symbols")])
    return InlineKeyboardMarkup(rows)


def _set_numeric(db: ControlDB, lang: str, key: str, raw_value: str) -> str:
    try:
        value = float(raw_value)
        if value <= 0:
            return t("invalid_number", lang)
    except ValueError:
        return t("invalid_number", lang)

    if key in INT_KEYS:
        value = int(value)
        if key == "max_leverage" and value > MAX_SAFE_LEVERAGE:
            return t("leverage_too_high", lang, max=MAX_SAFE_LEVERAGE)

    db.set_setting(key, value)
    return t("value_updated", lang, key=key, value=value)


def _status_text(db: ControlDB, context: ContextTypes.DEFAULT_TYPE, lang: str) -> str:
    s = db.get_settings()
    live = db.get_live_state()
    positions = live.get("open_positions", [])
    strat = STRATEGIES_BY_KEY.get(s.get("strategy", "balanced"))
    strat_label = (strat["label_fa"] if lang == "fa" else strat["label_en"]) if strat else s.get("strategy")
    configured = _configured_llm_providers()
    ai_label = (
        ("پایپ‌لاین ۴ مرحله‌ای (" + ", ".join(sorted(configured)) + ")") if configured
        else ("پایپ‌لاین ۴ مرحله‌ای (⚠️ بدون کلید)")
    ) if lang == "fa" else (
        ("4-stage pipeline (" + ", ".join(sorted(configured)) + ")") if configured
        else "4-stage pipeline (⚠️ no key set)"
    )
    return t(
        "status", lang,
        network=context.bot_data.get("network_label", "testnet"),
        capital=live.get("balance_usd", "?"), equity=s.get("capital_usd"),
        spot=live.get("spot_usd", "?"), perp=live.get("perp_usd", "?"),
        open_count=len(positions), today_pnl=round(db.today_pnl(), 2),
        model=ai_label, strategy=strat_label,
        symbols=", ".join(s.get("symbols", [])), max_leverage=s.get("max_leverage"),
        risk_pct=s.get("risk_per_trade_pct"),
        max_daily_loss_pct=s.get("max_daily_loss_pct"),
        max_consecutive_losses=s.get("max_consecutive_losses"),
        paused="Yes" if s.get("paused") else "No",
    )


STRATEGIES_BY_KEY = {x["key"]: x for x in STRATEGIES}

PERIODS = {
    "hourly": 3600,
    "daily": 86400,
    "weekly": 7 * 86400,
    "monthly": 30 * 86400,
}


def _pnl_text(db: ControlDB, lang: str, period: str, app=None) -> str:
    # Pull real trade history from Hyperliquid (authoritative source)
    account = (app.bot_data.get("hl_account") if app else None) or db.get_settings().get("hl_account")
    network = (app.bot_data.get("hl_network") if app else None) or db.get_settings().get("hl_network", "testnet")
    if account:
        try:
            summary = pnl_for_period(account, network, period)
        except Exception as e:
            logger.warning(f"hl_pnl failed for {period}: {e}")
            summary = None
    else:
        summary = None

    if summary is None:
        # Fallback to local DB if HL unavailable
        seconds = PERIODS.get(period, 86400)
        summary = db.pnl_summary(time.time() - seconds)

    settings = db.get_settings()
    start_cap = float(settings.get("starting_capital_usd") or settings.get("capital_usd") or 1) or 1
    pnl_pct = round(summary["total_pnl"] / start_cap * 100, 2) if start_cap else 0.0
    period_label = {
        "hourly": "1h" if lang == "en" else "۱ ساعت اخیر",
        "daily": "24h" if lang == "en" else "۲۴ ساعت اخیر",
        "weekly": "7d" if lang == "en" else "۷ روز اخیر",
        "monthly": "30d" if lang == "en" else "۳۰ روز اخیر",
    }.get(period, period)
    return t(
        "pnl_report", lang, period=period_label, count=summary["count"],
        wins=summary["wins"], losses=summary["losses"], win_rate=summary["win_rate"],
        total_pnl=summary["total_pnl"], pnl_pct=pnl_pct, avg_win=summary["avg_win"],
        avg_loss=summary["avg_loss"], best_symbol=summary["best_symbol"], best_pnl=summary["best_pnl"],
        worst_symbol=summary["worst_symbol"], worst_pnl=summary["worst_pnl"],
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )


def _apikeys_keyboard(lang: str) -> InlineKeyboardMarkup:
    configured = _configured_llm_providers()
    rows = []
    # Regular providers (non-GOD MODE)
    regular_providers = {k: v for k, v in LLM_PROVIDERS.items() if not k.startswith("godmode_") and k != "nvidia"}
    for prov, info in regular_providers.items():
        mark = "✅ " if prov in configured else "⚪️ "
        rows.append([InlineKeyboardButton(mark + info["label"], callback_data=f"setllm:{prov}")])
    # GOD MODE group - single button
    godmode_keys = ["godmode_openrouter", "godmode_gemini", "godmode_nvidia"]
    godmode_done = all(k in configured for k in godmode_keys)
    mark = "✅ " if godmode_done else "⚪️ "
    rows.append([InlineKeyboardButton(mark + "👑 GOD MODE (۳ کلید)", callback_data="setllm_godmode")])
    # NVIDIA separate (for the nvidia strategy)
    if "nvidia" in LLM_PROVIDERS:
        mark = "✅ " if "nvidia" in configured else "⚪️ "
        rows.append([InlineKeyboardButton(mark + LLM_PROVIDERS["nvidia"]["label"], callback_data="setllm:nvidia")])
    rows.append([InlineKeyboardButton(t("btn_back", lang), callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def _apikeys_text(lang: str) -> str:
    configured = _configured_llm_providers()
    lines = [
        "🔑 <b>کلیدهای هوش مصنوعی</b>" if lang == "fa" else "🔑 <b>AI provider keys</b>",
        "",
        ("<b>استراتژی‌های معمولی</b> — پاخ، بک‌آپ‌ها:" if lang == "fa" else "<b>Regular strategies</b> — pipeline, fallbacks:"),
    ]
    # Regular providers
    for prov, info in LLM_PROVIDERS.items():
        if prov.startswith("godmode_") or prov == "nvidia":
            continue
        mark = "✅ " if prov in configured else "⚪️ "
        lines.append(f"{mark}{info['label']}")
    lines.append("")
    lines.append(
        "<b>👑 GOD MODE</b> — ۳ کلید اختصاصی (هر مدل در گاد مود کلید خودش رو داره):" if lang == "fa"
        else "<b>👑 GOD MODE</b> — 3 dedicated keys (each model in GOD MODE has its own key):"
    )
    for prov in ["godmode_openrouter", "godmode_gemini", "godmode_nvidia"]:
        mark = "✅ " if prov in configured else "⚪️ "
        lines.append(f"  {mark}{LLM_PROVIDERS[prov]['label']}")
    lines.append("")
    if "nvidia" in LLM_PROVIDERS:
        mark = "✅ " if "nvidia" in configured else "⚪️ "
        lines.append(f"{mark}{LLM_PROVIDERS['nvidia']['label']} — برای استراتژی انویدیا")
    lines.append("")
    lines.append(
        "روی هر آیتم بزن → کلید رو بفرست. کلیدها جدا ذخیره میشن، موتور خودکار ری‌استارت میشه."
        if lang == "fa"
        else "Tap an item → send the key. Keys are saved separately, engine auto-restarts."
    )
    return "\n".join(lines)


def _strategy_keyboard(lang: str) -> InlineKeyboardMarkup:
    rows = []
    # OpenRouter group first
    rows.append([InlineKeyboardButton(
        ("🔵 اوپن‌روتر (زیرمجموعه توکن)") if lang == "fa" else ("🔵 OpenRouter (token submenu)"),
        callback_data="noop")])
    for st in STRATEGIES:
        if st.get("group") != "openrouter":
            continue
        label = st["label_fa"] if lang == "fa" else st["label_en"]
        # or_low / or_high open a SUB-MENU of personas rather than setting directly
        cb = f"strat_mode:{st['mode']}" if st.get("mode") else f"setstrategy:{st['key']}"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])
    # Other providers group
    rows.append([InlineKeyboardButton(
        ("🟢 سایر ارائه‌دهندگان") if lang == "fa" else ("🟢 Other providers"),
        callback_data="noop")])
    for st in STRATEGIES:
        if st.get("group") != "other":
            continue
        label = st["label_fa"] if lang == "fa" else st["label_en"]
        rows.append([InlineKeyboardButton(label, callback_data=f"setstrategy:{st['key']}")])
    rows.append([InlineKeyboardButton("✖️ " + ("بستن" if lang == "fa" else "Close"), callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def _strategy_persona_keyboard(mode: str, lang: str) -> InlineKeyboardMarkup:
    """Sub-menu: list all personas under a given OpenRouter token mode
    (or_low / or_high). Each button sets 'or_low__<persona>' etc."""
    rows = []
    header = ("⚡ مصرف کم (۱۵۰ توکن)") if mode == "or_low" else ("🔥 مصرف بالا (۳۰۰ توکن)")
    rows.append([InlineKeyboardButton(
        (f"🔵 اوپن‌روتر — {header}") if lang == "fa" else (f"🔵 OpenRouter — {header}"),
        callback_data="noop")])
    for p in OR_PERSONAS:
        label = p["label_fa"] if lang == "fa" else p["label_en"]
        rows.append([InlineKeyboardButton(label, callback_data=f"setstrategy:{mode}__{p['key']}")])
    rows.append([InlineKeyboardButton("↩️ " + ("بازگشت" if lang == "fa" else "Back"), callback_data="strategy")])
    return InlineKeyboardMarkup(rows)


def _strategy_text(lang: str) -> str:
    lines = ["🧠 <b>Analysis strategies</b>" if lang == "en" else "🧠 <b>استراتژی‌های تحلیل</b>", ""]
    lines.append("🔵 OpenRouter:" if lang == "en" else "🔵 اوپن‌روتر:")
    for st in STRATEGIES:
        if st.get("group") != "openrouter":
            continue
        label = st["label_fa"] if lang == "fa" else st["label_en"]
        desc = st["desc_fa"] if lang == "fa" else st["desc_en"]
        lines.append(f"• <b>{label}</b>\n  {desc}")
    lines.append("")
    lines.append("🟢 Other providers:" if lang == "en" else "🟢 سایر ارائه‌دهندگان:")
    for st in STRATEGIES:
        if st.get("group") != "other":
            continue
        label = st["label_fa"] if lang == "fa" else st["label_en"]
        desc = st["desc_fa"] if lang == "fa" else st["desc_en"]
        lines.append(f"• <b>{label}</b>\n  {desc}")
    lines.append("")
    lines.append("Tap a button to select." if lang == "en" else "برای انتخاب روی دکمه بزن.")
    return "\n".join(lines)


def _loop_interval_keyboard(db: ControlDB, lang: str) -> InlineKeyboardMarkup:
    s = db.get_settings()
    current = int(s.get("loop_interval_seconds", 300) // 60)
    rows = []
    for opt in LOOP_INTERVALS:
        mark = "✅ " if opt["minutes"] == current else ""
        label = (mark + (opt["label_fa"] if lang == "fa" else opt["label_en"]))
        rows.append([InlineKeyboardButton(label, callback_data=f"setinterval:{opt['minutes']}")])
    return InlineKeyboardMarkup(rows)


def _loop_interval_text(lang: str) -> str:
    if lang == "fa":
        return ("⏱ <b>فاصله بررسی (حلقه)</b>\n\n"
                "هرچه بزرگ‌تر، ریکوئست کمتر به APIهای هوش مصنوعی.\n"
                "بدون ری‌استارت اعمال می‌شود.")
    return ("⏱ <b>Poll interval (loop)</b>\n\n"
            "Larger = fewer API requests to the AI providers.\n"
            "Applied live, no restart needed.")


# ---------------- command handlers ----------------

import asyncio

async def _send_temp(app, chat_id: str, text: str, reply_markup=None, parse_mode=None, delay: float = 4.0):
    """Send a transient message that auto-deletes itself after `delay`
    seconds. Used for confirmations / error notices so the chat stays clean
    without any DB-backed message tracking."""
    kwargs = {}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    if parse_mode is not None:
        kwargs["parse_mode"] = parse_mode
    sent = await app.bot.send_message(chat_id, text, **kwargs)
    async def _delete_later():
        await asyncio.sleep(delay)
        try:
            await app.bot.delete_message(chat_id, sent.message_id)
        except Exception:
            pass
    asyncio.create_task(_delete_later())
    return sent

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    lang = _lang(db)
    # Single panel message (no extra "welcome" clutter) so chat stays clean.
    await update.message.reply_text(t("menu_title", lang), reply_markup=build_menu(db, lang))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    await update.message.reply_text(t("help", _lang(db)), parse_mode=ParseMode.HTML)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    lang = _lang(db)
    await update.message.reply_text(_status_text(db, context, lang), parse_mode=ParseMode.HTML)


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    lang = _lang(db)
    s = db.get_settings()
    text = t(
        "settings_dump", lang,
        capital_usd=s.get("capital_usd"), starting_capital_usd=s.get("starting_capital_usd"),
        max_leverage=s.get("max_leverage"), risk_per_trade_pct=s.get("risk_per_trade_pct"),
        max_daily_loss_pct=s.get("max_daily_loss_pct"), max_consecutive_losses=s.get("max_consecutive_losses"),
        min_notional_usd=s.get("min_notional_usd"), sizing_mode=s.get("sizing_mode"),
        trade_capital_pct=s.get("trade_capital_pct"),
        ai_keys=(", ".join(sorted(_configured_llm_providers())) or ("هیچ‌کدام ⚠️" if lang == "fa" else "none ⚠️")),
        strategy=s.get("strategy"),
        symbols=", ".join(s.get("symbols", [])), timeframes=", ".join(s.get("timeframes", [])),
        report_interval_hours=s.get("report_interval_hours"), language=s.get("language"),
        paused="Yes" if s.get("paused") else "No",
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    lang = _lang(db)
    positions = db.get_live_state().get("open_positions", [])
    if not positions:
        await update.message.reply_text(t("no_positions", lang))
        return
    lines = [
        f"{p['symbol']} {p['side'].upper()} | size={p['size']} entry={p['entry_price']} "
        f"uPnL={round(p['unrealized_pnl'], 2)}"
        for p in positions
    ]
    await update.message.reply_text("\n".join(lines))


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    live = db.get_live_state()
    spot = live.get("spot_usd", "?")
    perp = live.get("perp_usd", "?")
    total = live.get("balance_usd", "?")
    await update.message.reply_text(
        f"💰 موجودی کل: {total}$\n"
        f"━━━━━━━━━━━━\n"
        f"🪙 اسپات (Spot): {spot}$\n"
        f"📊 پرپ/فیوچرز (Perp): {perp}$\n"
        f"━━━━━━━━━━━━\n"
        f"برای انتقال: /transfer toperp 10  یا  /transfer tospot 10"
    )


async def transfer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    args = context.args
    usage = (
        "استفاده:\n"
        "/transfer toperp <مقدار>  → از اسپات به پرپ (برای معامله فیوچرز)\n"
        "/transfer tospot <مقدار>  → از پرپ به اسپات\n"
        "مثال: /transfer toperp 20"
    )
    if not args or len(args) < 2 or args[0] not in ("toperp", "tospot"):
        await update.message.reply_text(usage)
        return
    direction = args[0]
    try:
        amount = float(args[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("مقدار باید یک عدد مثبت باشه.")
        return
    db.enqueue_command(f"transfer:{direction}:{amount}")
    where = "پرپ/فیوچرز" if direction == "toperp" else "اسپات"
    await update.message.reply_text(
        f"🔄 درخواست انتقال {amount}$ → {where} ثبت شد.\n"
        f"موتور اصلی طی چند ثانیه اجراش می‌کنه و تأیید می‌فرسته."
    )


async def trades_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    lang = _lang(db)
    n = 10
    if context.args and context.args[0].isdigit():
        n = int(context.args[0])
    trades = db.recent_trades(n)
    if not trades:
        await update.message.reply_text(t("no_trades", lang))
        return
    lines = [
        f"{tr['symbol']} {tr['side']} | {tr['status']} | entry={tr['entry_price']} pnl={tr['pnl_usd']}"
        for tr in trades
    ]
    await update.message.reply_text("\n".join(lines))


async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    lang = _lang(db)
    n = 15
    if context.args and context.args[0].isdigit():
        n = int(context.args[0])
    logs = db.recent_logs(n)
    text = "\n".join(f"[{lg['level']}] {lg['message']}" for lg in logs) or t("no_trades", lang)
    await update.message.reply_text(text[:4000])


async def pnl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    lang = _lang(db)
    args = context.args
    if not args or args[0] not in PERIODS:
        await update.message.reply_text(t("pnl_usage", lang))
        return
    await update.message.reply_text(_pnl_text(db, lang, args[0], context.application), parse_mode=ParseMode.HTML)


async def reportevery_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    lang = _lang(db)
    args = context.args
    if not args:
        await update.message.reply_text(t("report_every_usage", lang))
        return
    try:
        hours = float(args[0])
        if hours < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(t("invalid_number", lang))
        return
    db.set_setting("report_interval_hours", hours)
    _reschedule_auto_report(context.application, hours)
    await update.message.reply_text(t("report_every_set", lang, h=hours))


async def killswitch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    s = db.get_settings()
    await update.message.reply_text(
        f"Today PnL: {round(db.today_pnl(), 2)}\n"
        f"Consecutive losses: {db.consecutive_losses_today()}\n"
        f"Limits: daily loss {s.get('max_daily_loss_pct')}% | "
        f"consecutive losses {s.get('max_consecutive_losses')}\n"
        f"Currently paused: {s.get('paused')}"
    )


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    db.set_setting("paused", True)
    await update.message.reply_text(t("paused", _lang(db)))


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    lang = _lang(db)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(t("yes", lang), callback_data="confirm_resume"),
        InlineKeyboardButton(t("no", lang), callback_data="cancel"),
    ]])
    await update.message.reply_text(t("confirm_resume", lang), reply_markup=kb)


async def closeall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    lang = _lang(db)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(t("yes", lang), callback_data="confirm_closeall"),
        InlineKeyboardButton(t("no", lang), callback_data="cancel"),
    ]])
    await update.message.reply_text(t("confirm_closeall", lang), reply_markup=kb)


async def restart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    lang = _lang(db)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(t("yes", lang), callback_data="confirm_restart"),
        InlineKeyboardButton(t("no", lang), callback_data="cancel"),
    ]])
    await update.message.reply_text(t("confirm_restart", lang), reply_markup=kb)


def _make_set_command(key: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        db: ControlDB = context.bot_data["db"]
        if not authorized(update, context.bot_data["allowed_chat_id"]):
            return
        lang = _lang(db)
        if not context.args:
            await update.message.reply_text(t("invalid_number", lang))
            return
        await update.message.reply_text(_set_numeric(db, lang, key, context.args[0]))
    return handler


async def sizingmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    args = context.args
    if not args or args[0] not in ("auto", "fixed"):
        cur = db.get_settings().get("sizing_mode", "auto")
        await update.message.reply_text(
            f"حالت فعلی اندازه معامله: {cur}\n"
            "━━━━━━━━━━━━\n"
            "🤖 auto  = هوش تصمیم می‌گیره چند درصد سرمایه وارد هر معامله بشه (تا سقف)\n"
            "✋ fixed = خودت درصد ثابت رو تعیین می‌کنی\n\n"
            "استفاده: /sizingmode auto  یا  /sizingmode fixed\n"
            "تعیین درصد/سقف: /settradepct 50"
        )
        return
    db.set_setting("sizing_mode", args[0])
    label = "🤖 هوشمند (auto)" if args[0] == "auto" else "✋ دستی (fixed)"
    await update.message.reply_text(f"✅ حالت اندازه معامله: {label}")


async def settradepct_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    args = context.args
    if not args:
        await update.message.reply_text("استفاده: /settradepct <عدد ۱ تا ۱۰۰>\nمثال: /settradepct 40")
        return
    try:
        pct = float(args[0])
        if not (1 <= pct <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("عدد باید بین ۱ تا ۱۰۰ باشه.")
        return
    db.set_setting("trade_capital_pct", pct)
    mode = db.get_settings().get("sizing_mode", "auto")
    role = "سقف مجاز برای هوش" if mode == "auto" else "درصد ثابت هر معامله"
    await update.message.reply_text(f"✅ {role} = {pct}٪ تنظیم شد.")


async def apikeys_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    lang = _lang(db)
    await update.message.reply_text(_apikeys_text(lang), reply_markup=_apikeys_keyboard(lang), parse_mode=ParseMode.HTML)


async def setapikey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setapikey with no args shows the provider buttons (same as the menu).
    /setapikey <provider> <key> saves directly for scripting/automation."""
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    lang = _lang(db)
    args = context.args
    if not args:
        await update.message.reply_text(_apikeys_text(lang), reply_markup=_apikeys_keyboard(lang), parse_mode=ParseMode.HTML)
        return
    if len(args) < 2 or args[0] not in LLM_PROVIDERS:
        providers = "/".join(LLM_PROVIDERS.keys())
        await update.message.reply_text(
            f"استفاده: /setapikey <{providers}> <کلید>" if lang == "fa"
            else f"Usage: /setapikey <{providers}> <key>"
        )
        return
    provider, key = args[0], args[1]
    try:
        await update.message.delete()  # keep the raw key out of chat history
    except Exception:
        pass
    try:
        _save_llm_key(provider, key)
        label = LLM_PROVIDERS[provider]["label"]
        msg = (f"✅ کلید {label} ذخیره شد! از سیکل بعدی هوش خودکار اعمال می‌شه (بدون ری‌استارت)."
               if lang == "fa" else f"✅ {label} key saved! Applied automatically on the next AI cycle (no restart).")
        await context.bot.send_message(update.effective_chat.id, msg)
    except Exception as e:
        logger.error(f"llm key save failed: {e}")
        await context.bot.send_message(
            update.effective_chat.id,
            (f"❌ خطا در ذخیره: {e}" if lang == "fa" else f"❌ Save failed: {e}"),
        )


async def strategy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    lang = _lang(db)
    await update.message.reply_text(_strategy_text(lang), reply_markup=_strategy_keyboard(lang), parse_mode=ParseMode.HTML)


async def symbols_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    lang = _lang(db)
    args = context.args
    s = db.get_settings()
    current = s.get("symbols", [])
    if not args:
        await update.message.reply_text(t("symbols_current", lang, symbols=", ".join(current)))
        return
    if args[0] == "set" and len(args) > 1:
        new_symbols = [x.strip().upper() for x in args[1].split(",") if x.strip()]
        db.set_setting("symbols", new_symbols)
        await update.message.reply_text(t("symbols_current", lang, symbols=", ".join(new_symbols)))
        return
    if args[0] == "add" and len(args) > 1:
        sym = args[1].strip().upper()
        if sym not in current:
            current.append(sym)
        db.set_setting("symbols", current)
        await update.message.reply_text(t("symbols_current", lang, symbols=", ".join(current)))
        return
    if args[0] == "remove" and len(args) > 1:
        sym = args[1].strip().upper()
        current = [x for x in current if x != sym]
        db.set_setting("symbols", current)
        await update.message.reply_text(t("symbols_current", lang, symbols=", ".join(current) or "-"))
        return
    await update.message.reply_text(t("symbols_usage", lang))


async def timeframes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    lang = _lang(db)
    args = context.args
    s = db.get_settings()
    if not args:
        await update.message.reply_text(t("timeframes_current", lang, timeframes=", ".join(s.get("timeframes", []))))
        return
    if args[0] == "set" and len(args) > 1:
        new_tfs = [x.strip() for x in args[1].split(",") if x.strip()]
        db.set_setting("timeframes", new_tfs)
        await update.message.reply_text(t("timeframes_current", lang, timeframes=", ".join(new_tfs)))
        return
    await update.message.reply_text(t("timeframes_usage", lang))


async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    if not context.args or context.args[0] not in ("en", "fa"):
        await update.message.reply_text("Usage: /lang en|fa")
        return
    db.set_setting("language", context.args[0])
    await update.message.reply_text(t("value_updated", context.args[0], key="language", value=context.args[0]))


# ---------------- inline button + free-text handlers ----------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    query = update.callback_query
    await query.answer()
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return

    lang = _lang(db)
    data = query.data

    if data == "toggle_lang":
        new_lang = "fa" if lang == "en" else "en"
        db.set_setting("language", new_lang)
        await query.edit_message_text(t("menu_title", new_lang), reply_markup=build_menu(db, new_lang))
        return

    if data == "toggle_network":
        s = db.get_settings()
        current_network = s.get("network", "testnet")
        new_network = "mainnet" if current_network == "testnet" else "testnet"

        # Guard: cannot switch to Mainnet unless its private key is set.
        # Read the engine config to check for a mainnet key.
        with open(ENGINE_CONFIG_PATH) as f:
            engine_cfg = yaml.safe_load(f)
        if new_network == "mainnet":
            net_cfg = (engine_cfg.get("hyperliquid", {}) or {}).get("mainnet", {}) or {}
            mainnet_addr = net_cfg.get("account_address", "")
            if not mainnet_addr:
                # Refuse to switch — do NOT change network, do NOT restart.
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔑 " + ("ست پریوات کی مین‌نت" if lang == "fa" else "Set Mainnet key"),
                                         callback_data="setkey_network:mainnet"),
                    InlineKeyboardButton("↩️ " + ("بازگشت" if lang == "fa" else "Back"),
                                         callback_data="main_menu"),
                ]])
                await query.edit_message_text(
                    ("⛔ نمی‌تونی به <b>🔴 Mainnet</b> بری چون هنوز کلید خصوصی (private key) مین‌نت رو ست نکردی.\n\n"
                     "اول از دکمه «🔑 ست پریوات کی مین‌نت» استفاده کن، بعد دوباره روی دکمه شبکه بزن."
                     if lang == "fa" else
                     "⛔ You can't switch to <b>🔴 Mainnet</b> because its private key isn't set yet.\n\n"
                     "Use the «🔑 Set Mainnet key» button first, then press the network button again."),
                    reply_markup=kb, parse_mode=ParseMode.HTML)
                return

        # --- safe to switch ---
        db.set_setting("network", new_network)
        # Also update the core_engine config.yaml to persist the network
        # NOTE: yaml is imported at module level (line 18) — do NOT re-import
        # locally here, or Python treats it as a local var for the whole
        # button_handler function and breaks yaml.safe_load() in other branches.
        engine_cfg["network"] = new_network
        with open(ENGINE_CONFIG_PATH, "w") as f:
            yaml.safe_dump(engine_cfg, f, allow_unicode=True, sort_keys=False)
        # Queue restart for engine so it picks up the new network
        db.enqueue_command("restart_engine")
        # Show confirmation
        network_label = "🔴 Mainnet" if new_network == "mainnet" else "🧪 Testnet"
        await query.edit_message_text(t("network_switched", lang, network=network_label))
        # Self-replace this process cleanly as a module (so relative imports work)
        os.execv(sys.executable, [sys.executable, "-m", "src.bot"])
        return

    if data == "toggle_pause":
        db.set_setting("paused", True)
        await refresh_to_menu(query, db, lang)
        return

    if data == "ask_resume":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(t("yes", lang), callback_data="confirm_resume"),
            InlineKeyboardButton(t("no", lang), callback_data="cancel"),
        ]])
        await query.message.reply_text(t("confirm_resume", lang), reply_markup=kb)
        return

    if data == "confirm_resume":
        db.set_setting("paused", False)
        await query.edit_message_text(t("resumed", lang), reply_markup=build_menu(db, lang))
        return

    if data == "ask_closeall":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(t("yes", lang), callback_data="confirm_closeall"),
            InlineKeyboardButton(t("no", lang), callback_data="cancel"),
        ]])
        await query.message.reply_text(t("confirm_closeall", lang), reply_markup=kb)
        return

    if data == "confirm_closeall":
        db.enqueue_command("closeall")
        await query.edit_message_text(t("closeall_queued", lang), reply_markup=build_menu(db, lang))
        return

    if data == "ask_restart":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(t("yes", lang), callback_data="confirm_restart"),
            InlineKeyboardButton(t("no", lang), callback_data="cancel"),
        ]])
        await query.message.reply_text(t("confirm_restart", lang), reply_markup=kb)
        return

    if data == "ask_restart_ctl":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(t("yes", lang), callback_data="confirm_restart_ctl"),
            InlineKeyboardButton(t("no", lang), callback_data="cancel"),
        ]])
        await query.message.reply_text(t("confirm_restart_ctl", lang), reply_markup=kb)
        return

    if data == "ask_setkey":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🧪 Testnet", callback_data="setkey_network:testnet"),
            InlineKeyboardButton("🔴 Mainnet", callback_data="setkey_network:mainnet"),
        ], [
            InlineKeyboardButton("✖️ " + ("بستن" if lang == "fa" else "Close"), callback_data="main_menu")
        ]])
        await query.edit_message_text(t("ask_setkey", lang), reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data.startswith("setkey_network:"):
        network = data.split(":", 1)[1]
        PENDING_EDIT[update.effective_chat.id] = f"__apikey__{network}"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✖️ " + ("بستن" if lang == "fa" else "Close"), callback_data="main_menu")
        ]])
        await query.edit_message_text(t("ask_setkey_network", lang, network=network.capitalize()), reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data == "confirm_restart":
        db.enqueue_command("restart_engine")
        await query.edit_message_text(t("restart_queued", lang))
        os.execv(sys.executable, [sys.executable, "-m", "src.bot"])
        return

    if data == "confirm_restart_ctl":
        # Show waiting state (no OK button yet)
        await query.edit_message_text(t("restart_ctl_waiting", lang))
        # remember this message id so post-restart cleanup can delete it
        try:
            db.set_setting("pending_ctl_restart_msg", query.message.message_id)
        except Exception:
            pass
        # Self-replace this process cleanly as a module (so relative imports work)
        os.execv(sys.executable, [sys.executable, "-m", "src.bot"])
        return

    if data == "cancel":
        await query.edit_message_text(t("cancelled", lang), reply_markup=build_menu(db, lang))
        return

    if data == "main_menu":
        await refresh_to_menu(query, db, lang)
        return
    if data == "risk_mode_menu":
        await risk_mode_menu(update, context)
        return
    if data.startswith("risk_mode:"):
        await risk_mode_set(update, context)
        return
    if data == "transfer_menu":
        await transfer_menu(update, context)
        return
    if data.startswith("transfer:"):
        await transfer_do(update, context)
        return
    if data == "adv_menu":
        await adv_menu(update, context)
        return
    if data == "adv_apply_claude":
        await adv_apply_claude(update, context)
        return
    if data == "adv_set_cap":
        PENDING_ADV_CAP[update.effective_chat.id] = True
        lang = _lang(db)
        await query.edit_message_text(
            "🔢 عدد سقف معاملات روزانه هر نماد را بنویس (۱ تا ۱۰):" if lang == "fa" else "🔢 Type daily trade cap per symbol (1-10):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ " + ("بازگشت" if lang == "fa" else "Back"), callback_data="adv_menu")]]))
        return

    if data == "status":
        text, kb = menu_with_ok(lang, _status_text(db, context, lang))
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data == "settings":
        s = db.get_settings()
        # starting_capital_usd is only shown if the user explicitly set it to
        # something other than the current capital ceiling (otherwise it's just
        # noise / a hardcoded default the user never chose)
        cap = s.get("capital_usd")
        start_cap = s.get("starting_capital_usd")
        start_cap_disp = start_cap if (start_cap not in (None, "", cap)) else None
        text = t(
            "settings_dump", lang,
            capital_usd=cap, starting_capital_usd=start_cap_disp,
            max_leverage=s.get("max_leverage"), risk_per_trade_pct=s.get("risk_per_trade_pct"),
            max_daily_loss_pct=s.get("max_daily_loss_pct"), max_consecutive_losses=s.get("max_consecutive_losses"),
            min_notional_usd=s.get("min_notional_usd"), sizing_mode=s.get("sizing_mode"),
            trade_capital_pct=s.get("trade_capital_pct"),
            ai_keys=(", ".join(sorted(_configured_llm_providers())) or ("هیچ‌کدام ⚠️" if lang == "fa" else "none ⚠️")),
            strategy=s.get("strategy"),
            symbols=", ".join(s.get("symbols", [])), timeframes=", ".join(s.get("timeframes", [])),
            report_interval_hours=s.get("report_interval_hours"), language=s.get("language"),
            paused="Yes" if s.get("paused") else "No",
        )
        _, kb = menu_with_ok(lang, text)
        # Hide the "starting capital" line entirely if it equals the ceiling
        # (user never explicitly set a separate starting capital)
        if start_cap_disp in (None, "", cap):
            import re
            text = re.sub(r"\(start:.*?\)\\n", "", text)
            text = re.sub(r"\(سرمایه‌ی اولیه:.*?\)\\n", "", text)
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data == "pnl_menu":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(t("btn_pnl_hourly", lang), callback_data="pnl:hourly"),
             InlineKeyboardButton(t("btn_pnl_daily", lang), callback_data="pnl:daily")],
            [InlineKeyboardButton(t("btn_pnl_weekly", lang), callback_data="pnl:weekly"),
             InlineKeyboardButton(t("btn_pnl_monthly", lang), callback_data="pnl:monthly")],
            [InlineKeyboardButton(t("btn_back", lang), callback_data="main_menu")],
        ])
        await query.edit_message_text(t("pnl_report_title", lang), reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data.startswith("pnl:"):
        period = data.split(":", 1)[1]
        text, kb = menu_with_ok(lang, _pnl_text(db, lang, period, context.application))
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data == "apikeys":
        await query.edit_message_text(_apikeys_text(lang), reply_markup=_apikeys_keyboard(lang), parse_mode=ParseMode.HTML)
        return

    if data == "test_quick":
        # Quick health check: just verify each key returns 200 (no real analysis)
        await query.edit_message_text(
            ("🔄 در حال تست سریع کلیدها..." if lang == "fa" else "🔄 Quick key check..."),
            reply_markup=build_menu(db, lang),
        )
        quick_result = _test_all_tokens_text(lang)
        menu_kb = build_menu(db, lang)
        ok_row = [InlineKeyboardButton("✅ " + ("بستن نتیجه" if lang == "fa" else "OK / Clear"), callback_data="test_ok")]
        rows = list(menu_kb.inline_keyboard) + [ok_row]
        await query.edit_message_text(
            t("menu_title", lang) + "\n\n" + quick_result,
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "test_tokens":
        await query.edit_message_text(
            ("🔄 در حال تست توکن‌ها و مدل‌ها..." if lang == "fa" else "🔄 Testing AI tokens & models..."),
            reply_markup=build_menu(db, lang),
        )
        token_result = _test_all_tokens_text(lang)
        model_result = _test_models_text(lang)
        combined = token_result + "\n\n" + model_result
        # Show result BELOW the main panel, with an OK button to dismiss it
        menu_kb = build_menu(db, lang)
        ok_row = [InlineKeyboardButton("✅ " + ("بستن نتیجه" if lang == "fa" else "OK / Clear"), callback_data="test_ok")]
        rows = list(menu_kb.inline_keyboard) + [ok_row]
        await query.edit_message_text(
            t("menu_title", lang) + "\n\n" + combined,
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "test_ok":
        # Dismiss the test result, return to the clean main panel
        await query.edit_message_text(t("menu_title", lang), reply_markup=build_menu(db, lang), parse_mode=ParseMode.HTML)
        return

    if data.startswith("setllm:"):
        prov = data.split(":", 1)[1]
        if prov not in LLM_PROVIDERS:
            await query.answer("پروایدر نامعتبر!" if lang == "fa" else "Invalid provider!", show_alert=True)
            return
        PENDING_EDIT[update.effective_chat.id] = f"__llmkey__:{prov}"
        info = LLM_PROVIDERS[prov]
        hint = info["hint"] if lang == "fa" else info["hint_en"]
        if lang == "fa":
            msg = (f"🔑 کلید جدید <b>{info['label']}</b> رو به‌صورت پیام بفرست:\n\n"
                   f"<i>{hint}</i>")
        else:
            msg = (f"🔑 Send the new <b>{info['label']}</b> API key as a message:\n\n"
                   f"<i>{hint}</i>")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✖️ " + ("بستن" if lang == "fa" else "Close"), callback_data="main_menu")
        ]])
        sent = await query.edit_message_text(msg, reply_markup=kb, parse_mode=ParseMode.HTML)
        # Remember which message is the prompt (the edited panel) so text_handler
        # can edit IT back to the menu instead of spawning a second panel.
        try:
            PENDING_EDIT_MENU_MSG[update.effective_chat.id] = query.message.message_id
        except Exception:
            pass
        return

    if data == "setllm_godmode":
        # Show sub-menu for the 3 GOD MODE keys
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                ("✅ " if "godmode_openrouter" in _configured_llm_providers() else "⚪️ ") + LLM_PROVIDERS["godmode_openrouter"]["label"],
                callback_data="setllm:godmode_openrouter"
            )],
            [InlineKeyboardButton(
                ("✅ " if "godmode_gemini" in _configured_llm_providers() else "⚪️ ") + LLM_PROVIDERS["godmode_gemini"]["label"],
                callback_data="setllm:godmode_gemini"
            )],
            [InlineKeyboardButton(
                ("✅ " if "godmode_nvidia" in _configured_llm_providers() else "⚪️ ") + LLM_PROVIDERS["godmode_nvidia"]["label"],
                callback_data="setllm:godmode_nvidia"
            )],
            [InlineKeyboardButton(t("btn_back", lang), callback_data="ask_setkey")]
        ])
        await query.edit_message_text(
            "👑 <b>GOD MODE — کلیدهای اختصاصی</b>\n\nهر مدل در گاد مود کلید خودش رو داره. جداگانه ست کن:" if lang == "fa"
            else "👑 <b>GOD MODE — Dedicated keys</b>\n\nEach model in GOD MODE has its own key. Set individually:",
            reply_markup=kb, parse_mode=ParseMode.HTML
        )
        return

    if data == "strategy":
        await query.edit_message_text(_strategy_text(lang), reply_markup=_strategy_keyboard(lang), parse_mode=ParseMode.HTML)
        return

    if data.startswith("strat_mode:"):
        mode = data.split(":", 1)[1]
        # open the persona sub-menu for this token mode (or_low / or_high)
        await query.edit_message_text(
            (_strategy_text(lang) + "\n\n" + ("🧠 <b>انتخاب استراتژی (شخصیت معاملاتی)</b>" if lang == "fa"
                                             else "🧠 <b>Pick a trading persona</b>")),
            reply_markup=_strategy_persona_keyboard(mode, lang),
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("setstrategy:"):
        key = data.split(":", 1)[1]
        # key may be "or_low__scalper" or "or_high__god_mode" (token mode + persona)
        # or a plain provider key like "nvidia"/"gemini"
        db.set_setting("strategy", key)
        # build a human-readable label
        if "__" in key:
            mode, persona = key.split("__", 1)
            mode_label = "⚡ مصرف کم" if mode == "or_low" else "🔥 مصرف بالا"
            persona_meta = next((p for p in OR_PERSONAS if p["key"] == persona), None)
            p_label = persona_meta["label_fa"] if (persona_meta and lang == "fa") else (persona_meta["label_en"] if persona_meta else persona)
            label = f"🔵 اوپن‌روتر — {mode_label} / {p_label}" if lang == "fa" else f"🔵 OpenRouter — {mode_label} / {p_label}"
            desc = persona_meta["desc_fa"] if (persona_meta and lang == "fa") else (persona_meta["desc_en"] if persona_meta else "")
        else:
            st = STRATEGIES_BY_KEY.get(key, {})
            label = st.get("label_fa" if lang == "fa" else "label_en", key)
            desc = st.get("desc_fa" if lang == "fa" else "desc_en", "")
        await query.edit_message_text(t("strategy_set", lang, label=label, desc=desc), reply_markup=build_menu(db, lang))
        return

    if data == "loop_interval":
        await query.edit_message_text(_loop_interval_text(lang), reply_markup=_loop_interval_keyboard(db, lang), parse_mode=ParseMode.HTML)
        return

    if data.startswith("setinterval:"):
        minutes = int(data.split(":", 1)[1])
        db.set_setting("loop_interval_seconds", minutes * 60)
        cur = "⏱ فاصله بررسی" if lang == "fa" else "⏱ Poll interval"
        await query.answer(
            (f"فاصله روی {minutes} دقیقه تنظیم شد" if lang == "fa" else f"Interval set to {minutes} min")
        )
        # confirmation shown BELOW the panel (not replacing it), with a back button
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ " + ("بازگشت به پنل" if lang == "fa" else "Back to panel"), callback_data="main_menu")
        ]])
        await query.message.reply_text(
            f"✅ {cur}: <b>{minutes}</b> {'دقیقه' if lang == 'fa' else 'min'}\n"
            + ("از چرخه‌ی بعد اعمال می‌شود (بدون ری‌استارت)." if lang == "fa"
               else "Applied on the next cycle (no restart)."),
            reply_markup=kb, parse_mode=ParseMode.HTML,
        )
        return

    if data == "balance_hl":
        await query.edit_message_text(
            ("🔄 در حال دریافت موجودی از هایپرلیکوئید..." if lang == "fa" else "🔄 Fetching balance from Hyperliquid..."),
            reply_markup=build_menu(db, lang),
        )
        try:
            # Read network from DB (single source of truth)
            settings = db.get_settings()
            current_network = settings.get("network", "testnet")
            hl_cfg = yaml.safe_load(open(ENGINE_CONFIG_PATH))
            net_cfg = (hl_cfg.get("hyperliquid", {}) or {}).get(current_network, {}) or {}
            sec = net_cfg.get("secret_key", "")
            addr = net_cfg.get("account_address", "")
            net = current_network
            if not addr or not sec:
                msg = (f"🔑 کلید خصوصی برای شبکه <b>{net}</b> ست نشده.\n\n"
                       f"از منوی اصلی «🔑 Set exchange key» رو بزن و کلید همین شبکه رو وارد کن."
                       if lang == "fa" else
                       f"🔑 No private key set for network <b>{net}</b>.\n\n"
                       f"Use «🔑 Set exchange key» from the main menu.")
                await query.edit_message_text(msg, reply_markup=build_menu(db, lang), parse_mode=ParseMode.HTML)
                return
            from hyperliquid_client import HyperliquidClient
            client = HyperliquidClient(addr, sec, net)
            total = client.get_usdc_balance()
            # جزئیات بیشتر برای نمایش تمیز
            spot_free = 0.0
            try:
                sp = client.info.spot_user_state(addr)
                for b in sp.get("balances", []):
                    if b.get("coin") == "USDC":
                        spot_free += float(b.get("total", 0)) - float(b.get("hold", 0))
            except Exception:
                pass
            perp_val = max(total - spot_free, 0.0)
            if lang == "fa":
                text = (f"💰 <b>موجودی (هایپرلیکوئید - {net})</b>\n"
                        f"━━━━━━━━━━━━\n"
                        f"🪙 اسپات (آزاد): {spot_free:.2f}$\n"
                        f"📊 پرپ/فیوچرز: {perp_val:.2f}$\n"
                        f"━━━━━━━━━━━━\n"
                        f"💵 کل: <b>{total:.2f}$</b>")
            else:
                text = (f"💰 <b>Balance (Hyperliquid - {net})</b>\n"
                        f"━━━━━━━━━━━━\n"
                        f"🪙 Spot (free): {spot_free:.2f}$\n"
                        f"📊 Perp/Futures: {perp_val:.2f}$\n"
                        f"━━━━━━━━━━━━\n"
                        f"💵 Total: <b>{total:.2f}$</b>")
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✖️ " + ("بستن" if lang == "fa" else "Close"), callback_data="main_menu")
            ]])
            await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception as e:
            err = (f"❌ خطا در دریافت موجودی: {e}" if lang == "fa"
                   else f"❌ Balance fetch failed: {e}")
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✖️ " + ("بستن" if lang == "fa" else "Close"), callback_data="main_menu")
            ]])
            await query.edit_message_text(err, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data == "full_report":
        await query.edit_message_text(
            ("🔄 در حال جمع‌آوری گزارش جامع از هایپرلیکوئید..." if lang == "fa"
             else "🔄 Building full report from Hyperliquid..."),
            reply_markup=build_menu(db, lang),
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 " + ("بروزرسانی" if lang == "fa" else "Refresh"), callback_data="full_report"),
            InlineKeyboardButton("✖️ " + ("بستن" if lang == "fa" else "Close"), callback_data="main_menu"),
        ]])
        try:
            # Read network from DB (single source of truth)
            settings = db.get_settings()
            current_network = settings.get("network", "testnet")
            hl_cfg = yaml.safe_load(open(ENGINE_CONFIG_PATH))
            net_cfg = (hl_cfg.get("hyperliquid", {}) or {}).get(current_network, {}) or {}
            addr = net_cfg.get("account_address", "")
            net = current_network
            if not addr:
                # No API key set for this network yet — don't crash, just inform
                msg = (f"🔑 کلید خصوصی (private key) برای شبکه <b>{net}</b> ست نشده.\n\n"
                       f"لطفاً از منوی اصلی دکمه «🔑 Set exchange key» رو بزن و کلید مربوط به "
                       f"همین شبکه رو وارد کن، بعد دوباره گزارش بگیر."
                       if lang == "fa" else
                       f"🔑 No private key is set for network <b>{net}</b>.\n\n"
                       f"Use the «🔑 Set exchange key» button from the main menu and enter the "
                       f"key for this network, then request the report again.")
                await query.edit_message_text(msg, reply_markup=kb, parse_mode=ParseMode.HTML)
                return
            report = build_comprehensive(addr, net, lang)
            await query.edit_message_text(report[:4000], reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"full_report failed: {e}")
            err = (f"❌ خطا در ساخت گزارش: {e}" if lang == "fa" else f"❌ Report failed: {e}")
            await query.edit_message_text(err, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data == "reports_menu":
        await query.edit_message_text(
            _reports_menu_text(db, lang),
            reply_markup=reports_menu_keyboard(lang, db),
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("rep_toggle:"):
        key = data.split(":", 1)[1]
        _toggle_report(db, key, lang)
        # refresh the panel in place (edit, no new message) — use the
        # combined Reports & Alerts menu, NOT the old standalone alerts menu
        await query.edit_message_text(
            _reports_menu_text(db, lang),
            reply_markup=reports_menu_keyboard(lang, db),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "symbols":
        text = _symbols_menu_text(db, lang)
        await query.edit_message_text(text, reply_markup=symbols_menu_keyboard(lang), parse_mode=ParseMode.HTML)
        return

    if data == "sym_add":
        await query.edit_message_text(t("symbols_pick_add", lang), reply_markup=_symbol_picker_keyboard(db, lang, "add"))
        return

    if data == "sym_remove":
        await query.edit_message_text(t("symbols_pick_remove", lang), reply_markup=_symbol_picker_keyboard(db, lang, "remove"))
        return

    if data.startswith("sym_add:"):
        sym = data.split(":", 1)[1].upper()
        current = db.get_settings().get("symbols", [])
        if sym in [x.upper() for x in current]:
            await query.answer(t("symbols_already", lang, symbol=sym))
            return
        current.append(sym)
        db.set_setting("symbols", current)
        await query.answer(t("symbols_added", lang, symbol=sym))
        # refresh the picker (symbol now excluded) in place
        await query.edit_message_text(t("symbols_pick_add", lang), reply_markup=_symbol_picker_keyboard(db, lang, "add"))
        return

    if data.startswith("sym_remove:"):
        sym = data.split(":", 1)[1].upper()
        current = db.get_settings().get("symbols", [])
        if sym not in [x.upper() for x in current]:
            await query.answer(t("symbols_not_active", lang, symbol=sym))
            return
        new = [x for x in current if x.upper() != sym]
        db.set_setting("symbols", new)
        await query.answer(t("symbols_removed", lang, symbol=sym))
        await query.edit_message_text(t("symbols_pick_remove", lang), reply_markup=_symbol_picker_keyboard(db, lang, "remove"))
        return

    if data == "report":
        trades = db.recent_trades(10)
        text = t("no_trades", lang) if not trades else "\n".join(
            f"{tr['symbol']} {tr['side']} | {tr['status']} | entry={tr['entry_price']} pnl={tr['pnl_usd']}"
            for tr in trades
        )
        _, kb = menu_with_ok(lang, text)
        await query.message.reply_text(text, reply_markup=kb)
        return

    if data == "logs":
        logs = db.recent_logs(40)
        # Keep only important entries (errors + warnings) so the panel stays useful
        important = [lg for lg in logs if lg.get("level") in ("ERROR", "WARNING", "CRITICAL")]
        if not important:
            important = logs[:10]  # fall back to most recent if nothing important
        lines = []
        for lg in important:
            lvl = lg.get("level", "INFO")
            icon = {"ERROR": "🔴", "CRITICAL": "🔴", "WARNING": "🟡"}.get(lvl, "⚪️")
            ts = lg.get("ts", 0)
            try:
                tstr = datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
            except Exception:
                tstr = ""
            msg = str(lg.get("message", ""))[:160].replace("\n", " ")
            lines.append(f"{icon} <code>{tstr}</code> {msg}")
        text = "📋 <b>لاگ‌های اخیر</b> (مهم‌ترین‌ها)\n━━━━━━━━━━━━\n" + "\n".join(lines) if lang == "fa" else \
               "📋 <b>Recent logs</b> (most important)\n━━━━━━━━━━━━\n" + "\n".join(lines)
        _, kb = menu_with_ok(lang, text)
        await query.edit_message_text(text[:4000], reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data.startswith("edit:"):
        key = data.split(":", 1)[1]
        PENDING_EDIT[update.effective_chat.id] = key
        # Send a separate prompt message (panel stays untouched) and remember
        # its id so we can delete it once the user replies (keeps chat clean)
        prompt = await query.message.reply_text(t("enter_new_value", lang))
        PENDING_EDIT_PROMPT[update.effective_chat.id] = prompt.message_id
        return


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.bot_data["db"]
    if not authorized(update, context.bot_data["allowed_chat_id"]):
        return
    chat_id = update.effective_chat.id
    # --- advanced: daily trade cap flow ---
    if chat_id in PENDING_ADV_CAP:
        PENDING_ADV_CAP.pop(chat_id)
        lang = _lang(db)
        raw = update.message.text.strip()
        try:
            val = int(raw)
        except Exception:
            await update.message.reply_text("⚠️ عدد صحیح وارد کن (۱ تا ۱۰)." if lang == "fa" else "⚠️ Enter a valid number (1-10).")
            return
        if val < 1 or val > 10:
            await update.message.reply_text("⚠️ عدد باید بین ۱ تا ۱۰ باشد." if lang == "fa" else "⚠️ Number must be between 1 and 10.")
            return
        db.set_setting("max_trades_per_symbol_per_day", val)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ " + ("بازگشت" if lang == "fa" else "Back"), callback_data="adv_menu")]])
        await update.message.reply_text(
            f"✅ <b>سقف معاملات روزانه هر نماد: {val} معامله</b>\n\n"
            f"این فقط سقف مجاز است؛ تایید نهایی با فیلترهای هوشمند (rule_engine) است." if lang == "fa" else
            f"✅ <b>Daily trade cap per symbol: {val} trades</b>\n\nThis is only a ceiling; final approval is by the smart filters (rule_engine).",
            parse_mode="HTML", reply_markup=kb)
        return
    # --- transfer amount flow ---
    if chat_id in PENDING_TRANSFER:
        pend = PENDING_TRANSFER.pop(chat_id)
        lang = _lang(db)
        raw = update.message.text.strip().replace(",", ".")
        try:
            amt = float(raw)
        except Exception:
            await update.message.reply_text("⚠️ عدد نامعتبر. دوباره از منوی انتقالات تلاش کن." if lang == "fa" else "⚠️ Invalid number. Try again from the transfer menu.")
            return
        try:
            from hyperliquid_client import HyperliquidClient
        except Exception:
            from hyperliquid_client import HyperliquidClient
        try:
            hl = HyperliquidClient(account_address=cfg_secret_address(context), secret_key=cfg_secret_key(context), network=db.get_settings().get("network", "testnet"))
            r = hl.transfer_between_wallets(amt, pend["to_perp"])
            if r.get("ok"):
                txt = (f"✅ <b>{r['msg']}</b>\n\n"
                       f"SPOT: {r['spot_after']:.2f} | PERP: {r['perp_after']:.2f} USDC")
            else:
                txt = f"❌ {r.get('msg','خطا')}"
        except Exception as e:
            txt = f"❌ خطا: {e}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ " + ("بازگشت" if lang == "fa" else "Back"), callback_data="transfer_menu")]])
        await update.message.reply_text(txt, parse_mode="HTML", reply_markup=kb)
        return
    if chat_id not in PENDING_EDIT:
        return
    key = PENDING_EDIT.pop(chat_id)
    lang = _lang(db)

    # Delete the user's typed message to keep chat clean
    try:
        await update.message.delete()
    except Exception:
        pass

    # Delete the "enter new value" prompt we showed earlier
    prompt_id = PENDING_EDIT_PROMPT.pop(chat_id, None)
    if prompt_id is not None:
        try:
            await context.bot.delete_message(chat_id, prompt_id)
        except Exception:
            pass

    # LLM provider API key setup flow
    if key.startswith("__llmkey__:"):
        prov = key.split(":", 1)[1]
        raw = update.message.text.strip()
        info = LLM_PROVIDERS.get(prov, {})
        pref = info.get("prefix", "")
        # The prompt message we want to edit back into the menu (no 2nd panel)
        menu_msg_id = PENDING_EDIT_MENU_MSG.pop(chat_id, None)

        async def _finish(text_msg):
            """Edit the prompt message back into the panel; fall back to a
            fresh tracked panel only if editing fails."""
            if menu_msg_id is not None:
                try:
                    await context.bot.edit_message_text(
                        text_msg, chat_id=chat_id, message_id=menu_msg_id,
                        reply_markup=build_menu(db, lang), parse_mode=ParseMode.HTML)
                    return
                except Exception:
                    pass
            await _send_temp(context.application, str(chat_id), text_msg,
                             reply_markup=build_menu(db, lang), parse_mode=ParseMode.HTML)

        if pref and not raw.startswith(pref):
            await _finish(
                f"⚠️ این با فرمت {info.get('label', prov)} نمی‌خونه (باید با {pref} شروع شه)."
                if lang == "fa" else
                f"⚠️ That doesn't look like a {info.get('label', prov)} key (must start with {pref}).")
            return
        try:
            _save_llm_key(prov, raw)
            msg = (f"✅ توکن {info.get('label', prov)} ذخیره شد! از سیکل بعدی هوش خودکار اعمال می‌شه (بدون ری‌استارت)."
                   if lang == "fa" else
                   f"✅ {info.get('label', prov)} key saved! Applied automatically on the next AI cycle (no restart).")
            await _finish(msg)
        except Exception as e:
            logger.error(f"llm key save failed: {e}")
            await _finish(f"❌ خطا در ذخیره: {e}" if lang == "fa" else f"❌ Save failed: {e}")
        return

    # API key setup flow
    if key.startswith("__apikey__"):
        # Parse network from key: "__apikey__" or "__apikey__testnet" or "__apikey__mainnet"
        net = "testnet"
        if key != "__apikey__":
            net = key.split("__", 2)[-1]  # get network part
        raw = update.message.text.strip()
        import re
        menu_msg_id = PENDING_EDIT_MENU_MSG.pop(chat_id, None)

        async def _finish_apikey(text_msg):
            if menu_msg_id is not None:
                try:
                    await context.bot.edit_message_text(
                        text_msg, chat_id=chat_id, message_id=menu_msg_id,
                        reply_markup=build_menu(db, lang), parse_mode=ParseMode.HTML)
                    return
                except Exception:
                    pass
            await _send_temp(context.application, str(chat_id), text_msg,
                             reply_markup=build_menu(db, lang), parse_mode=ParseMode.HTML)

        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", raw):
            await _finish_apikey(t("invalid_key", lang))
            return
        # Derive account address from the key and save to engine config
        try:
            from eth_account import Account
            addr = Account.from_key(raw).address
            _save_api_key(raw, addr, network=net)
            await _finish_apikey(t("key_saved", lang))
            # Restart engine to pick up the new key
            db.enqueue_command("restart_engine")
        except Exception as e:
            logger.error(f"api key save failed: {e}")
            await _finish_apikey(t("invalid_key", lang))
        return

    # Save the value. Flow: delete prompt -> show "saving" -> edit to
    # "saved" -> auto-delete both so the panel stays intact in chat.
    # Delete the "enter new value" prompt message
    try:
        await update.message.delete()
    except Exception:
        pass

    reply = _set_numeric(db, lang, key, update.message.text.strip())

    # Show a transient "saving" message, then flip to "saved"
    status = None
    try:
        status = await _send_temp(context.application, str(chat_id), t("saving", lang), delay=2.0)
    except Exception:
        status = None

    if status is not None:
        try:
            await status.edit_text(t("saved_short", lang))
        except Exception:
            pass
        # auto-delete the "saved" message after a few seconds so it doesn't linger
        async def _cleanup(msg):
            await asyncio.sleep(3)
            try:
                await msg.delete()
            except Exception:
                pass
        if status is not None:
            asyncio.create_task(_cleanup(status))


# ---------------- scheduled auto P&L report ----------------

async def _auto_report_job(context: ContextTypes.DEFAULT_TYPE):
    db: ControlDB = context.application.bot_data["db"]
    chat_id = context.application.bot_data["allowed_chat_id"]
    lang = _lang(db)
    await context.bot.send_message(chat_id, _pnl_text(db, lang, "daily", context.application), parse_mode=ParseMode.HTML)


def _reschedule_auto_report(app: Application, hours: float):
    for job in app.job_queue.get_jobs_by_name("auto_pnl_report"):
        job.schedule_removal()
    if hours and hours > 0:
        app.job_queue.run_repeating(_auto_report_job, interval=hours * 3600, first=hours * 3600, name="auto_pnl_report")


async def _post_init(app: Application):
    """Runs once after the bot connects: registers the '/' command menu
    (fixes it not showing up in Telegram's UI) in both languages, and
    starts the auto P&L report job from whatever is currently saved.

    NOTE: We no longer auto-wipe the chat on every (re)start. That cleanup
    logic (tracking every sent message in the DB and deleting them on
    restart) caused stale/duplicate messages to pile up in the user's chat
    whenever the bot restarted. Now we just drop a fresh panel without
    touching prior messages.
    """
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS_EN])
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS_FA], language_code="fa")
    db: ControlDB = app.bot_data["db"]
    chat_id = app.bot_data.get("allowed_chat_id")
    hours = db.get_settings().get("report_interval_hours", 24)
    _reschedule_auto_report(app, hours)
    # Fresh panel (we no longer wipe prior messages — that caused duplicate
    # stale notices to accumulate on every restart).
    if chat_id:
        try:
            lang = db.get_settings().get("language", "en")
            # If we just restarted the panel, clean up the "restarting..." notice
            # and briefly show an "online" confirmation, then drop it.
            pending_msg = db.get_settings().get("pending_ctl_restart_msg")
            if pending_msg:
                try:
                    await app.bot.delete_message(chat_id, int(pending_msg))
                except Exception:
                    pass
                db.set_setting("pending_ctl_restart_msg", None)
                online_msg = await app.bot.send_message(
                    chat_id,
                    "✅ <b>پنل آنلاین شد</b>" if lang == "fa" else "✅ <b>Panel is online</b>",
                    parse_mode="HTML",
                )
                await asyncio.sleep(3)
                try:
                    await app.bot.delete_message(chat_id, online_msg.message_id)
                except Exception:
                    pass
            await app.bot.send_message(chat_id, t("menu_title", lang), reply_markup=build_menu(db, lang))
        except Exception as e:
            logger.warning(f"panel send failed: {e}")
    logger.info("Command menu registered, auto-report interval=%sh", hours)


def main():
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    # Make core_engine's hyperliquid_client importable from control_bot.
    # bot.py lives in control_bot/src/, target is trading_bot/core_engine/src,
    # so we go up TWO levels (.., ..) from __file__.
    _core = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "core_engine", "src"))
    if _core not in sys.path:
        sys.path.insert(0, _core)

    # now report_config (in core_engine/src) is importable
    from report_config import REPORTS, setting_key, ALERT_KEYS
    globals()["REPORTS"] = REPORTS
    globals()["setting_key"] = setting_key
    globals()["ALERT_KEYS"] = ALERT_KEYS

    db = ControlDB(cfg["shared_db_path"])
    # Ensure network setting exists in DB
    settings = db.get_settings()
    if "network" not in settings:
        db.set_setting("network", cfg.get("network", "testnet"))

    app = Application.builder().token(cfg["telegram_bot_token"]).post_init(_post_init).build()
    app.bot_data["db"] = db
    app.bot_data["allowed_chat_id"] = cfg["allowed_chat_id"]
    current_network = db.get_settings().get("network", cfg.get("network", "testnet"))
    app.bot_data["network_label"] = "🧪 Testnet" if current_network == "testnet" else "🔴 Mainnet"
    hl = cfg.get("hyperliquid", {})
    net_cfg = hl.get(current_network, {})
    app.bot_data["hl_account"] = net_cfg.get("account_address")
    app.bot_data["hl_network"] = current_network

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("positions", positions_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("transfer", transfer_cmd))
    app.add_handler(CommandHandler("trades", trades_cmd))
    app.add_handler(CommandHandler("logs", logs_cmd))
    app.add_handler(CommandHandler("pnl", pnl_cmd))
    app.add_handler(CommandHandler("reportevery", reportevery_cmd))
    app.add_handler(CommandHandler("killswitch", killswitch_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CommandHandler("closeall", closeall_cmd))
    app.add_handler(CommandHandler("restart", restart_cmd))
    app.add_handler(CommandHandler("setcapital", _make_set_command("capital_usd")))
    app.add_handler(CommandHandler("setstartcapital", _make_set_command("starting_capital_usd")))
    app.add_handler(CommandHandler("setleverage", _make_set_command("max_leverage")))
    app.add_handler(CommandHandler("setrisk", _make_set_command("risk_per_trade_pct")))
    app.add_handler(CommandHandler("setdailyloss", _make_set_command("max_daily_loss_pct")))
    app.add_handler(CommandHandler("setconsecutivelosses", _make_set_command("max_consecutive_losses")))
    app.add_handler(CommandHandler("sizingmode", sizingmode_cmd))
    app.add_handler(CommandHandler("settradepct", settradepct_cmd))
    app.add_handler(CommandHandler("models", apikeys_cmd))
    app.add_handler(CommandHandler("apikeys", apikeys_cmd))
    app.add_handler(CommandHandler("setapikey", setapikey_cmd))
    app.add_handler(CommandHandler("strategy", strategy_cmd))
    app.add_handler(CommandHandler("symbols", symbols_cmd))
    app.add_handler(CommandHandler("timeframes", timeframes_cmd))
    app.add_handler(CommandHandler("lang", lang_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("Control bot starting...")
    app.run_polling()


# ---------------------------------------------------------------------------
# Reports panel: toggle every report on/off. Essential reports (kill switch,
# AI rate-limit halt, engine errors, liquidation warning) are shown as locked
# and cannot be turned off because silencing them could cost real money.
# ---------------------------------------------------------------------------
def _reports_menu_text(db: ControlDB, lang: str) -> str:
    settings = db.get_settings()
    lines = []
    lines.append("🔔 <b>گزارش‌ها و هشدارها</b>" if lang == "fa" else "🔔 <b>Reports & Alerts</b>")
    lines.append("━━━━━━━━━━━━━━")
    # Reports section (non-alert items)
    lines.append("📋 " + ("گزارش‌ها" if lang == "fa" else "Reports"))
    for key, meta in REPORTS.items():
        if key in ALERT_KEYS:
            continue
        val = settings.get(setting_key(key))
        on = meta["default"] if val is None else bool(val)
        marker = "🟢" if on else "🔴"
        label = meta["fa"] if lang == "fa" else meta["en"]
        if meta["essential"]:
            lines.append(f"🔒 {label} <i>(ضروری)</i>")
        else:
            lines.append(f"{marker} {label}")
    # Alerts section
    lines.append("")
    lines.append("🚨 " + ("هشدارها" if lang == "fa" else "Alerts"))
    for key in ALERT_KEYS:
        meta = REPORTS.get(key)
        if not meta:
            continue
        val = settings.get(setting_key(key))
        on = meta["default"] if val is None else bool(val)
        marker = "🟢" if on else "🔴"
        label = meta["fa"] if lang == "fa" else meta["en"]
        lines.append(f"{marker} {label}")
    lines.append("")
    lines.append("▮ روشن  ▯ خاموش  🔒 ضروری (همیشه فعال)" if lang == "fa"
                 else "▮ on  ▯ off  🔒 essential (always on)")
    return "\n".join(lines)


def reports_menu_keyboard(lang: str, db: ControlDB = None) -> InlineKeyboardMarkup:
    rows = []
    # Reports section (non-alert items), 2-column
    items = [(k, m) for k, m in REPORTS.items() if k not in ALERT_KEYS]
    for i in range(0, len(items), 2):
        pair = items[i:i + 2]
        row = []
        for key, meta in pair:
            if meta["essential"]:
                continue
            on = meta["default"]
            if db is not None:
                val = db.get_settings().get(setting_key(key))
                on = meta["default"] if val is None else bool(val)
            marker = "🟢" if on else "🔴"
            label = meta["fa"] if lang == "fa" else meta["en"]
            row.append(InlineKeyboardButton(
                f"{marker} {label}",
                callback_data=f"rep_toggle:{key}",
            ))
        if row:
            rows.append(row)
    # Alerts section, 2-column
    rows.append([InlineKeyboardButton(
        "🚨 " + ("هشدارها" if lang == "fa" else "Alerts"),
        callback_data="noop")])
    alert_items = [(k, REPORTS[k]) for k in ALERT_KEYS if k in REPORTS]
    for i in range(0, len(alert_items), 2):
        pair = alert_items[i:i + 2]
        row = []
        for key, meta in pair:
            on = meta["default"]
            if db is not None:
                val = db.get_settings().get(setting_key(key))
                on = meta["default"] if val is None else bool(val)
            marker = "🟢" if on else "🔴"
            label = meta["fa"] if lang == "fa" else meta["en"]
            row.append(InlineKeyboardButton(
                f"{marker} {label}",
                callback_data=f"rep_toggle:{key}",
            ))
        if row:
            rows.append(row)
    rows.append([InlineKeyboardButton(
        "🔄 " + ("بروزرسانی" if lang == "fa" else "Refresh"), callback_data="reports_menu")])
    rows.append([InlineKeyboardButton(
        "✖️ " + ("بستن" if lang == "fa" else "Close"), callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def _toggle_report(db: ControlDB, key: str, lang: str) -> None:
    meta = REPORTS.get(key)
    if meta is None or meta["essential"]:
        return
    settings = db.get_settings()
    val = settings.get(setting_key(key))
    cur = meta["default"] if val is None else bool(val)
    db.set_setting(setting_key(key), not cur)


def _alerts_menu_text(db: ControlDB, lang: str) -> str:
    settings = db.get_settings()
    lines = []
    lines.append("🔔 <b>هشدارها</b>" if lang == "fa" else "🔔 <b>Alerts</b>")
    lines.append("━━━━━━━━━━━━━━")
    for key in ALERT_KEYS:
        meta = REPORTS.get(key)
        if not meta or meta["essential"]:
            continue
        val = settings.get(setting_key(key))
        on = meta["default"] if val is None else bool(val)
        marker = "🟢" if on else "🔴"
        label = meta["fa"] if lang == "fa" else meta["en"]
        lines.append(f"{marker} {label}")
    lines.append("")
    lines.append("🟢 روشن  🔴 خاموش" if lang == "fa" else "🟢 on  🔴 off")
    return "\n".join(lines)


def alerts_menu_keyboard(lang: str, db: ControlDB = None) -> InlineKeyboardMarkup:
    rows = []
    # 2-column layout; full label + tiny on/off marker
    for i in range(0, len(ALERT_KEYS), 2):
        pair = ALERT_KEYS[i:i + 2]
        row = []
        for key in pair:
            meta = REPORTS.get(key)
            if not meta or meta["essential"]:
                continue
            on = meta["default"]
            if db is not None:
                val = db.get_settings().get(setting_key(key))
                on = meta["default"] if val is None else bool(val)
            marker = "🟢" if on else "🔴"
            label = meta["fa"] if lang == "fa" else meta["en"]
            row.append(InlineKeyboardButton(
                f"{marker} {label}",
                callback_data=f"rep_toggle:{key}",
            ))
        if row:
            rows.append(row)
    rows.append([InlineKeyboardButton(
        "🔄 " + ("بروزرسانی" if lang == "fa" else "Refresh"), callback_data="alerts_menu")])
    rows.append([InlineKeyboardButton(
        "✖️ " + ("بستن" if lang == "fa" else "Close"), callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


if __name__ == "__main__":
    main()
