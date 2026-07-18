"""
Display-only copies of the fixed AI pipeline description and strategy list
from core_engine/src/ai_pipeline.py. Kept as a separate copy (not a shared
import) for the same reason as shared_db.py -- control_bot can be
deployed/updated independently of core_engine. If you change a stage model
or a strategy in core_engine, update the matching entry here too so it
shows up correctly in the "AI keys" and /strategy menus.
"""

# The AI side is a FIXED 4-stage pipeline now -- nothing to pick here, this
# is shown purely as an info panel in the "AI keys" menu so the user knows
# what each key they add is actually used for.
PIPELINE_STAGES = [
    {"role_fa": "1️⃣ تحلیل چارت (تکنیکال)", "role_en": "1️⃣ Chart analyst (technical)",
     "model": "deepseek/deepseek-chat"},
    {"role_fa": "2️⃣ تحلیل فاندامنتال (فاندینگ/اوپن‌اینترست)", "role_en": "2️⃣ Fundamental analyst (funding/OI)",
     "model": "openai/gpt-4o"},
    {"role_fa": "3️⃣ ترکیب و امتیازدهی سیگنال", "role_en": "3️⃣ Synthesizer",
     "model": "google/gemini-2.5-flash"},
    {"role_fa": "4️⃣ تصمیم نهایی (ورود/عدم ورود + ریسک پیشنهادی)", "role_en": "4️⃣ Decision maker",
     "model": "anthropic/claude-3-haiku"},
]

# Providers whose key can be added from the "AI keys" menu. A single
# OpenRouter key alone is enough to power all 4 stages above (it proxies
# every vendor under one key); OpenAI/Gemini keys are optional direct
# fallbacks used automatically if OpenRouter isn't configured.
LLM_PROVIDERS = {
    "openrouter": {"label": "OpenRouter", "prefix": "sk-or-",
                   "hint": "کلید OpenRouter (شروع با sk-or-) — به‌تنهایی کل پایپ‌لاین رو فعال می‌کنه",
                   "hint_en": "OpenRouter key (starts with sk-or-) — alone powers the whole pipeline"},
    "openrouter_backup": {"label": "OpenRouter پشتیبان", "prefix": "sk-or-",
                   "hint": "کلید OpenRouter دوم (حساب جداگونه) — وقتی اصلی ریت‌لیمیت شد استفاده می‌شه",
                   "hint_en": "Second OpenRouter key (separate account) — used when the primary is rate-limited"},
    "openai": {"label": "OpenAI", "prefix": "sk-",
               "hint": "کلید OpenAI (شروع با sk-) — بک‌آپ مستقیم برای مراحل GPT",
               "hint_en": "OpenAI key (starts with sk-) — direct fallback for GPT stages"},
    "gemini": {"label": "Google Gemini", "prefix": "",
               "hint": "کلید Gemini (AIza... یا AQ...) — بک‌آپ مستقیم برای مرحله‌ی ترکیب",
               "hint_en": "Gemini key (AIza... or AQ...) — direct fallback for the synthesis stage"},
    "godmode_openrouter": {"label": "GOD MODE — OpenRouter", "prefix": "sk-or-",
                   "hint": "کلید OpenRouter اختصاصیِ GOD MODE (شروع با sk-or-) — جدا از کلید بالا، فقط برای Claude/GPT/DeepSeek در این استراتژی",
                   "hint_en": "Dedicated OpenRouter key for GOD MODE (starts with sk-or-) — separate from the one above, used only by Claude/GPT/DeepSeek in this strategy"},
    "godmode_gemini": {"label": "GOD MODE — Gemini", "prefix": "",
               "hint": "کلید Gemini اختصاصیِ GOD MODE (AIza... یا AQ...) — فقط برای تحلیل‌گر احساسات بازار در این استراتژی",
               "hint_en": "Dedicated Gemini key for GOD MODE (AIza... or AQ...) — used only by the sentiment analyst in this strategy"},
    "godmode_nvidia": {"label": "GOD MODE — NVIDIA (اختیاری)", "prefix": "nvapi-",
               "hint": "کلید NVIDIA NIM (اختیاری، شروع با nvapi-) — یک تحلیل‌گر پنجم و مستقل اضافه می‌کند",
               "hint_en": "NVIDIA NIM key (optional, starts with nvapi-) — adds a 5th, independent cross-check analyst"},
    "nvidia": {"label": "NVIDIA", "prefix": "nvapi-",
               "hint": "کلید NVIDIA NIM (شروع با nvapi-) — برای استراتژی انویدیا مستقیم",
               "hint_en": "NVIDIA NIM key (starts with nvapi-) — for direct NVIDIA strategy"},
}

# Fixed trading personas available under EACH OpenRouter token mode.
# or_low  = cheap models + 150-token cap (free tier)
# or_high = stronger models + 300-token cap (paid)
# Both share these persona keys; the engine splits them as "or_low__<key>"
# or "or_high__<key>" so it knows which model budget to use.
OR_PERSONAS = [
    {"key": "conservative", "label_fa": "🛡 محتاط", "label_en": "🛡 Conservative",
     "desc_fa": "فقط وقتی چند اندیکاتور هم‌جهت باشن وارد می‌شه؛ حد ضرر کوتاه، اطمینان بالا لازم داره.",
     "desc_en": "Enters only when multiple indicators agree; tight stops, needs high confidence."},
    {"key": "balanced", "label_fa": "⚖️ متعادل", "label_en": "⚖️ Balanced",
     "desc_fa": "پیش‌فرض خوب برای بیشتر شرایط؛ ریسک و فرصت را متوازن می‌کند.",
     "desc_en": "Good default for most conditions; balances risk and opportunity."},
    {"key": "aggressive", "label_fa": "🔥 تهاجمی", "label_en": "🔥 Aggressive",
     "desc_fa": "معاملات بیشتر، ورود سریع‌تر با شواهد جزئی؛ ریسک بالاتر برای فرصت بیشتر.",
     "desc_en": "More trades, enters on partial evidence; higher risk for more upside."},
    {"key": "scalper", "label_fa": "⚡ اسکالپر", "label_en": "⚡ Scalper",
     "desc_fa": "تایم‌فریم کوتاه، حد ضرس و سود بسیار کوچک، تعداد معاملات زیاد.",
     "desc_en": "Short-horizon, very tight SL/TP, high trade frequency."},
    {"key": "trend_follower", "label_fa": "📈 روندمحور", "label_en": "📈 Trend Follower",
     "desc_fa": "فقط هم‌جهت با روند غالب معامله می‌کند؛ حد سود بزرگ‌تر.",
     "desc_en": "Only trades with the dominant trend; larger targets."},
    {"key": "mean_reversion", "label_fa": "🔁 بازگشت به میانگین", "label_en": "🔁 Mean Reversion",
     "desc_fa": "روی افراط‌های قیمتی معامله می‌کند و انتظار بازگشت به میانگین دارد.",
     "desc_en": "Trades price extremes, expects reversion toward the mean."},
    {"key": "swing", "label_fa": "🌊 سوئینگ", "label_en": "🌊 Swing",
     "desc_fa": "افق زمانی بلندتر، نویز کوتاه‌مدت را نادیده می‌گیرد.",
     "desc_en": "Longer horizon, ignores short-term noise, wider SL/TP."},
    {"key": "god_mode", "label_fa": "👑 گاد مود", "label_en": "👑 GOD MODE",
     "desc_fa": "شورای ۴ مدل مستقل (Claude/GPT/Gemini/DeepSeek + NVIDIA اختیاری) با داور "
                "پایتونی قطعی؛ ورود فقط با توافق ≥۳ مدل و اطمینان میانگین >۷۲٪.",
     "desc_en": "Council of 4 independent models (Claude/GPT/Gemini/DeepSeek + optional "
                "NVIDIA) with a deterministic Python judge; enters only when >=3 models "
                "agree with >72% average confidence."},
]

STRATEGIES = [
    {"key": "or_low", "label_fa": "⚡ اوپن‌روتر — مصرف کم (رایگان)", "label_en": "⚡ OpenRouter — Low token (free)",
     "group": "openrouter", "mode": "or_low",
     "desc_fa": "مدل‌های ارزان اوپن‌روتر + سقف ۱۵۰ توکن. برای حساب‌های رایگان؛ کیفیت متوسط ولی هزینه صفر.",
     "desc_en": "Cheap OpenRouter models + 150-token cap. For free-tier accounts; decent quality, zero cost."},
    {"key": "or_high", "label_fa": "🔥 اوپن‌روتر — مصرف بالا (پولی)", "label_en": "🔥 OpenRouter — High token (paid)",
     "group": "openrouter", "mode": "or_high",
     "desc_fa": "مدل‌های قوی‌تر اوپن‌روتر (Gemini/Claude) + سقف ۳۰۰ توکن. کیفیت بهتر، نیاز به توکن پولی.",
     "desc_en": "Stronger OpenRouter models (Gemini/Claude) + 300-token cap. Better quality, needs paid token."},
    {"key": "nvidia", "label_fa": "🟢 انویدیا (NVIDIA)", "label_en": "🟢 NVIDIA",
     "group": "other",
     "desc_fa": "مدل‌های NVIDIA Nemotron/Llama روی کلید NVIDIA مستقیم — مستقل از OpenRouter.",
     "desc_en": "Runs NVIDIA Nemotron/Llama on direct NVIDIA key — independent of OpenRouter."},
    {"key": "gemini", "label_fa": "🌟 جمینی (Gemini)", "label_en": "🌟 Gemini",
     "group": "other",
     "desc_fa": "فقط با توکن Gemini کار می‌کند — مستقل از OpenRouter/OpenAI.",
     "desc_en": "Runs ONLY on the Gemini key — independent of OpenRouter/OpenAI."},
]

# Cycle interval options for the "⏱ فاصله بررسی" menu. Larger = fewer API
# requests per hour (used to throttle AI provider usage). Core engine reads
# loop_interval_seconds from settings every cycle, so changing it needs no
# restart.
LOOP_INTERVALS = [
    {"minutes": 5, "label_fa": "⚡ ۵ دقیقه (پیش‌فرض، ریکوئست بیشتر)", "label_en": "⚡ 5 min (default, more requests)"},
    {"minutes": 15, "label_fa": "🐢 ۱۵ دقیقه (متعادل)", "label_en": "🐢 15 min (balanced)"},
    {"minutes": 30, "label_fa": "🐌 ۳۰ دقیقه (ریکوئست کم)", "label_en": "🐌 30 min (fewer requests)"},
    {"minutes": 60, "label_fa": "🐌🐌 ۶۰ دقیقه (کمترین ریکوئست)", "label_en": "🐌🐌 60 min (least requests)"},
]
