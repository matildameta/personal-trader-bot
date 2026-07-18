"""
Fixed, multi-model AI pipeline. Replaces the old "pick one model" design.

Four AI calls run in sequence, each a different role, each on a FIXED
model -- nobody selects models anymore, there is nothing to pick:

  1. CHART ANALYST     -- reads the multi-timeframe technical indicators
                           only, produces a structured technical read.
  2. FUNDAMENTAL ANALYST -- reads funding rate / open interest / volume
                           (see fundamentals.py), produces a structured
                           positioning/sentiment read.
  3. SYNTHESIZER        -- combines (1) and (2) into one directional
                           signal with a strength score and an explicit
                           note on whether technical and fundamental agree
                           or conflict.
  4. DECISION MAKER     -- takes the synthesis and, using the ACTIVE
                           STRATEGY PERSONA (still user-selectable via
                           /strategy -- conservative/balanced/aggressive/
                           scalper/trend_follower/mean_reversion/swing),
                           proposes the final signal + confidence +
                           suggested capital%/SL/TP. Exactly like before,
                           this proposal is NOT the last word: risk_engine.py
                           deterministically enforces max_leverage, position
                           sizing and stop placement -- the AI never directly
                           controls money movement.

Every stage is reachable through OpenRouter with a single key (it proxies
Claude/GPT/DeepSeek/Gemini/etc under one API), which is why one OpenRouter
key is enough to run the whole combined pipeline. OpenAI and Gemini keys
are optional extras: if OpenRouter isn't configured, each stage falls back
to a direct call to whichever provider key IS available. Keys are supplied
via config.yaml -> llm.api_keys.{openrouter,openai,gemini} (each provider
saved to its own slot -- see control_bot's "AI keys" menu) and are only
read at process start, like the exchange key, so adding/replacing one
requires the usual restart_engine (control_bot does this for you).
"""
import json
import logging
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_fixed

from .god_mode import GodModeAnalyzer

logger = logging.getLogger("ai_pipeline")

# ---------------------------------------------------------------------------
# Fixed model assignment per pipeline stage. Not user-configurable by design
# -- the whole point is "combined and fixed", not "pick a model".
# Ids are OpenRouter-style (vendor/model); PROVIDER_BASE_URLS + _resolve()
# below translate to a direct-provider id automatically when only a direct
# key (openai/gemini) is available instead of an OpenRouter key.
# ---------------------------------------------------------------------------
# NOTE: model ids below are verified live on OpenRouter. The previous ids
# (google/gemini-2.0-flash-exp, anthropic/claude-3.5-sonnet) were removed by
# Shared default stage models (only used as a fallback if a strategy key
# is not found in STRATEGY_MODELS below). Kept cheap.
STAGE_MODELS = {
    "chart": "deepseek/deepseek-chat",
    "fundamental": "meta-llama/llama-3.1-8b-instruct",
    "synthesis": "deepseek/deepseek-chat",
    "decision": "meta-llama/llama-3.1-8b-instruct",
}

# Per-strategy model assignment under OpenRouter. "or_low" and "or_high"
# BOTH run on OpenRouter (primary key, then backup key) but differ in token
# budget / model quality:
#   - or_low  : cheap models + max_tokens=150  (free-tier friendly)
#   - or_high : stronger models + max_tokens=300 (paid OpenRouter)
# The "gemini" strategy uses ONLY the Gemini provider/direct key; "nvidia"
# uses ONLY the NVIDIA provider. If a strategy's required provider key is
# missing, that strategy is skipped (and reported) instead of failing.
STRATEGY_MODELS = {
    "or_low": {
        "provider": "openrouter",
        "stages": {
            "chart": "deepseek/deepseek-chat",
            "fundamental": "meta-llama/llama-3.1-8b-instruct",
            "synthesis": "deepseek/deepseek-chat",
            "decision": "meta-llama/llama-3.1-8b-instruct",
        },
    },
    "or_high": {
        "provider": "openrouter",
        "stages": {
            "chart": "deepseek/deepseek-chat",
            "fundamental": "google/gemini-2.5-flash",
            "synthesis": "google/gemini-2.5-flash",
            "decision": "anthropic/claude-3-haiku",
        },
    },
    "gemini": {
        "provider": "gemini",
        "stages": {
            "chart": "gemini-2.5-flash",
            "fundamental": "gemini-2.5-flash",
            "synthesis": "gemini-2.5-pro",
            "decision": "gemini-2.5-pro",
        },
    },
    "nvidia": {
        "provider": "nvidia",
        "stages": {
            "chart": "nvidia/nemotron-3-ultra-550b-a55b",
            "fundamental": "meta/llama-3.1-70b-instruct",
            "synthesis": "meta/llama-3.1-70b-instruct",
            "decision": "meta/llama-3.1-70b-instruct",
        },
    },
}

# --- BASE_RULES + persona variants (for __persona strategies) ---
BASE_RULES = """Crypto futures analyst. Given multi-TF indicators (low TF=timing, high TF=trend). Output ONLY JSON:
{"signal":"long"|"short"|"hold","confidence":0-1,"suggested_capital_pct":0-100,"suggested_stop_loss_pct":f,"suggested_take_profit_pct":f,"reasoning":"1 sentence"}
Favor HTF trend; lower confidence on conflicting indicators."""

_STAGE1_SYSTEM = """TECHNICAL stage. Multi-TF indicators (high TF=trend, low TF=timing). Output ONLY JSON:
{"technical_bias":"long"|"short"|"neutral","technical_confidence":0-1,"key_levels_notes":"1 sentence"}"""

_STAGE2_SYSTEM = """POSITIONING stage. Funding rate/OI/24h volume for a pair. High +funding=crowded longs(contrarian risk); very -funding=crowded shorts. Output ONLY JSON:
{"fundamental_bias":"long"|"short"|"neutral","fundamental_confidence":0-1,"notes":"1 sentence"}"""

_STAGE3_SYSTEM = """SYNTHESIS stage. Combine technical+fundamental bias. Agree=stronger; conflict=say so, lower strength, lean technical over positioning. Output ONLY JSON:
{"combined_bias":"long"|"short"|"neutral","signal_strength":0-100,"agreement":"aligned"|"conflicting","synthesis_notes":"1 sentence"}"""

# Persona variants appended to BASE_RULES for __persona strategies
PERSONA_RULES = {
    "conservative": BASE_RULES + """
Conservative: signal only if 3+ indicators align; else hold. conf<0.6 unless strong. cap_pct<=50. SL 0.8-1.5%, TP 1.5-2x SL.""",
    "balanced": BASE_RULES + """
Balanced: directional view on clear bias; hold only if contradictory. Scale conf/cap_pct to agreement. SL 1-2%, TP 2-4%.""",
    "aggressive": BASE_RULES + """
Aggressive: take any reasonable edge even if TFs partly disagree. Hold only if flatly contradictory/ATR very low. cap_pct up to 80. SL 1.5-3%, TP proportional.""",
    "scalper": BASE_RULES + """
Scalper: weight lowest TF heavily; higher TF only as trend filter. SL 0.4-1%, TP 0.6-1.5%. Prefer RSI-extreme/BB-rejection setups.""",
    "trend_follower": BASE_RULES + """
Trend follower: long only confirmed uptrend (px>EMA20>EMA50, ADX trending); short only confirmed downtrend. Disagreeing HTFs=hold. SL 1.5-2.5%, TP 3-6%.""",
    "mean_reversion": BASE_RULES + """
Mean reversion: fade RSI/BB extremes turning, only if HTF not strongly opposing. SL 1-2% beyond extreme, TP 1.5-3% toward EMA20.""",
    "swing": BASE_RULES + """
Swing: weight highest TF for direction; lowest TF minor nudge only. Hold if HTF unclear. SL 2-4%, TP 4-8%.""",
    "gemini": BASE_RULES + """
Concise structured reasoning; favor well-supported signals, lower conf on conflict; fewer high-conviction trades.""",
}

# strategy_name -> (base_model_key, persona_key)
# base_model_key must exist in STRATEGY_MODELS; persona_key in PERSONA_RULES
__PERSONA_STRATEGIES = {
    "or_high__conservative": ("or_high", "conservative"),
    "or_low__balanced": ("or_low", "balanced"),
    "or_high__trend_follower": ("or_high", "trend_follower"),
    "gemini__swing": ("gemini", "swing"),
    "nvidia__mean_reversion": ("nvidia", "mean_reversion"),
    "or_low__scalper": ("or_low", "scalper"),
    "god_mode": ("nvidia", "balanced"),
}

def models_for_strategy(strategy: str) -> dict:
    """Return the {stage: model_id} dict for a strategy.

    Accepted forms:
      - "or_low" / "or_high" ............ bare token mode (uses balanced persona)
      - "or_low__scalper" / "or_high__god_mode" ... token mode + persona
      - "nvidia" / "gemini" ............. direct provider strategies
    Any unknown key falls back to the cheap STAGE_MODELS.
    """
    # strip the persona suffix: "or_low__scalper" -> "or_low"
    base = strategy.split("__", 1)[0] if "__" in strategy else strategy
    spec = STRATEGY_MODELS.get(base)
    if spec:
        return dict(spec["stages"])
    return dict(STAGE_MODELS)


def persona_for_strategy(strategy: str) -> str:
    """Return the persona system prompt for a strategy (or BASE_RULES fallback)."""
    if strategy in __PERSONA_STRATEGIES:
        _, persona = __PERSONA_STRATEGIES[strategy]
        return PERSONA_RULES.get(persona, BASE_RULES)
    if "__" in strategy:
        persona = strategy.split("__", 1)[1]
        return PERSONA_RULES.get(persona, BASE_RULES)
    return BASE_RULES

PROVIDER_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "openrouter_backup": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "nvidia": "https://integrate.api.nvidia.com/v1",
}

# Detected when a provider returns 429 / rate-limit / quota-exceeded. The
# pipeline then falls back to the backup OpenRouter key, and if even that
# fails it reports a hard rate-limit and the engine stops trading.
_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "quota",
    "too many requests",
    "model is currently overloaded",
    "daily limit",
    "exceeded",
    "402",
    "credit",
    "credits",
    "more credits",
    "insufficient",
    "payment",
    "billing",
)


def _is_rate_limit(err: Exception) -> bool:
    msg = str(err).lower()
    return any(m in msg for m in _RATE_LIMIT_MARKERS)

# When a stage's fixed model has to be reached directly (no OpenRouter key),
# map "vendor prefix in the OpenRouter id" -> (provider key name, direct model id).
_DIRECT_VENDOR_MAP = {
    "openai": lambda model_id: ("openai", model_id.split("/", 1)[1]),
    "google": lambda model_id: ("gemini", model_id.split("/", 1)[1]),
    "nvidia": lambda model_id: ("nvidia", model_id),
    "meta": lambda model_id: ("nvidia", model_id),
}
# Best-effort substitute model per provider, used only when the stage's own
# vendor has no direct key AND OpenRouter is unavailable either (keeps the
# pipeline running in degraded form instead of skipping a stage entirely).
_SUBSTITUTE_MODEL = {
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
    "nvidia": "meta/llama-3.1-70b-instruct",
}

# Fallback chain per stage: if the primary fixed model is unavailable
# (404/401/403/429 hard-fail, not a transient network blip), try these in
# order. All ids verified live on OpenRouter 2026-07-15. This keeps the
# pipeline running even if one vendor removes a model, instead of skipping
# the whole trade.
STAGE_FALLBACK = {
    "chart": ["deepseek/deepseek-chat", "meta-llama/llama-3.1-8b-instruct"],
    "fundamental": ["meta-llama/llama-3.1-8b-instruct", "deepseek/deepseek-chat"],
    "synthesis": ["deepseek/deepseek-chat", "meta-llama/llama-3.1-8b-instruct"],
    "decision": ["meta-llama/llama-3.1-8b-instruct", "deepseek/deepseek-chat"],
}

BASE_RULES = """You are a crypto futures market analyst. You are given numeric
technical indicators for one symbol across multiple timeframes (lower
timeframe = entry timing, higher timeframe = trend context). You do NOT
decide position size, leverage, or execute trades -- a separate
deterministic risk engine does that; you only propose a signal.

Read the timeframes together, not in isolation: prefer trading in the
direction of the higher-timeframe trend, and use the lower timeframe to
judge whether entry timing is favorable right now (e.g. avoid buying into
an already-extended move against a higher-timeframe reversal signal).
Treat RSI/Stochastic extremes, EMA crossovers and slope, MACD histogram
direction, Bollinger Band position, ADX/trend-strength, and volume as
corroborating evidence -- a signal is stronger when several indicators
agree and weaker when they conflict. When conflicting, say so in your
reasoning and lower your confidence accordingly.

Respond with ONLY a single JSON object, no prose, no markdown fences,
matching exactly this schema:
{
  "signal": "long" | "short" | "hold",
  "confidence": <float 0.0-1.0>,
  "suggested_capital_pct": <float 0-100, how much of available capital to commit to THIS trade, based on conviction>,
  "suggested_stop_loss_pct": <float, % below/above entry, e.g. 1.5>,
  "suggested_take_profit_pct": <float, % above/below entry, e.g. 3.0>,
  "reasoning": "<2-3 concise sentences citing the specific indicators that drove the decision>"
}
"""

STRATEGIES = {
    "conservative": {
        "label_fa": "🛡 محتاط (Conservative)",
        "label_en": "🛡 Conservative",
        "desc_fa": "فقط وقتی چند اندیکاتور هم‌جهت باشن وارد می‌شه؛ حد ضرر کوتاه، اطمینان بالا لازم داره.",
        "desc_en": "Only enters when multiple indicators agree; tight stops, needs high confidence.",
        "prompt": BASE_RULES + """
STRATEGY PERSONA: Conservative. Only pick "long"/"short" when at least
three independent indicators (trend, momentum, volatility position) agree
on direction across timeframes. If evidence is mixed, use "hold" -- you
would rather miss a trade than take a low-quality one. Use confidence
below 0.6 for anything short of very clear alignment; never exceed 0.5
suggested_capital_pct. Prefer tighter stop_loss_pct (0.8-1.5%) and
take_profit_pct roughly 1.5-2x the stop.
""",
    },
    "balanced": {
        "label_fa": "⚖️ متعادل (Balanced)",
        "label_en": "⚖️ Balanced",
        "desc_fa": "پیش‌فرض خوب برای بیشتر شرایط؛ ریسک و فرصت را متوازن می‌کند.",
        "desc_en": "Good default for most conditions; balances risk and opportunity.",
        "prompt": BASE_RULES + """
STRATEGY PERSONA: Balanced. Take a directional view whenever the
indicators show a discernible bias across timeframes, and reserve "hold"
for genuinely contradictory signals. Size confidence and
suggested_capital_pct proportionally to how many indicators agree.
Typical stop_loss_pct 1-2%, take_profit_pct 2-4% (roughly 1:2 risk:reward
or better).
""",
    },
    "aggressive": {
        "label_fa": "🔥 تهاجمی (Aggressive)",
        "label_en": "🔥 Aggressive",
        "desc_fa": "معاملات بیشتر، ورود سریع‌تر با شواهد جزئی؛ ریسک بالاتر برای فرصت بیشتر.",
        "desc_en": "More trades, enters on partial evidence; higher risk for more upside.",
        "prompt": BASE_RULES + """
STRATEGY PERSONA: Aggressive. Favor taking a position whenever there is
any reasonable directional edge, even if not every timeframe agrees --
being early is more valuable than being certain. Use "hold" only when
signals are flatly contradictory or volatility (ATR) is unusually low.
suggested_capital_pct can go up to 80 on high-conviction setups.
Stop_loss_pct can be wider (1.5-3%) to avoid noise stop-outs, with
take_profit_pct proportionally larger.
""",
    },
    "scalper": {
        "label_fa": "⚡ اسکالپر (Scalper)",
        "label_en": "⚡ Scalper",
        "desc_fa": "تایم‌فریم کوتاه، حد ضرر و سود بسیار کوچک، تعداد معاملات زیاد.",
        "desc_en": "Short-horizon, very tight SL/TP, high trade frequency.",
        "prompt": BASE_RULES + """
STRATEGY PERSONA: Scalper. Weight the LOWEST timeframe most heavily --
you are trading short-term momentum and mean-reversion around it, using
higher timeframes only as a coarse directional filter (don't fight a
strong higher-timeframe trend). Use tight stop_loss_pct (0.4-1%) and
take_profit_pct (0.6-1.5%). Favor quick, high-probability setups (e.g.
RSI extremes reverting, price rejecting a Bollinger Band) over waiting
for perfect alignment.
""",
    },
    "trend_follower": {
        "label_fa": "📈 روندمحور (Trend Follower)",
        "label_en": "📈 Trend Follower",
        "desc_fa": "فقط هم‌جهت با روند غالب معامله می‌کند؛ حد سود بزرگ‌تر برای گرفتن کل روند.",
        "desc_en": "Only trades with the dominant trend; larger targets to ride the move.",
        "prompt": BASE_RULES + """
STRATEGY PERSONA: Trend follower. Only take "long" in a confirmed uptrend
(higher timeframe price above rising EMA20/EMA50, ADX showing trend
strength if available) and only "short" in a confirmed downtrend -- never
counter-trend. If the higher timeframes disagree with each other, "hold".
Use wider take_profit_pct (3-6%) relative to stop_loss_pct (1.5-2.5%) to
let winners run; this persona trades less often but aims for bigger moves.
""",
    },
    "mean_reversion": {
        "label_fa": "🔁 بازگشت به میانگین (Mean Reversion)",
        "label_en": "🔁 Mean Reversion",
        "desc_fa": "روی افراط‌های قیمتی معامله می‌کند و انتظار بازگشت به میانگین دارد.",
        "desc_en": "Trades price extremes, expects reversion toward the mean.",
        "prompt": BASE_RULES + """
STRATEGY PERSONA: Mean reversion. Look for price stretched away from its
mean -- e.g. RSI overbought/oversold, price outside Bollinger Bands,
extended distance from EMA20 -- with momentum showing early signs of
turning (MACD histogram flattening/reversing). Take "short" on overbought
extremes and "long" on oversold extremes, but only when the higher
timeframe is not in a strong opposing trend (avoid fading a strong
trend). Use moderate stop_loss_pct (1-2%) placed beyond the extreme, and
take_profit_pct targeting the mean (EMA20), typically 1.5-3%.
""",
    },
    "swing": {
        "label_fa": "🌊 سوئینگ (Swing)",
        "label_en": "🌊 Swing",
        "desc_fa": "افق زمانی بلندتر، نویز کوتاه‌مدت را نادیده می‌گیرد، حد ضرر و سود بزرگ‌تر.",
        "desc_en": "Longer horizon, ignores short-term noise, wider SL/TP.",
        "prompt": BASE_RULES + """
STRATEGY PERSONA: Swing trader. Weight the HIGHEST available timeframe
most heavily for direction and treat the lowest timeframe only as a
minor entry-timing nudge -- ignore short-term noise. Prefer fewer, larger
trades: stop_loss_pct 2-4%, take_profit_pct 4-8%. Use "hold" if the
higher timeframe trend itself is unclear, regardless of what the lower
timeframe is doing.
""",
    },
    "gemini": {
        "label_fa": "🌟 جمینی (Gemini)",
        "label_en": "🌟 Gemini",
        "desc_fa": "فقط با توکن Gemini کار می‌کند، تحلیل سریع و دقیق چندمرحله‌ای.",
        "desc_en": "Runs exclusively on the Gemini key — fast, multi-step analysis.",
        "prompt": BASE_RULES + """STRATEGY PERSONA: Gemini-native. You are powered by Google Gemini and
should leverage its strengths: fast, structured technical reading and
clear, concise reasoning. Favor well-supported signals over speculation;
when indicators conflict, say so and lower confidence. Keep stop/target
sizing consistent with the broader risk rules above. Prefer fewer, higher
conviction trades and explain your reasoning in 1-2 tight sentences.
""",
    },
    "god_mode": {
        "label_fa": "👑 گاد مود (GOD MODE)",
        "label_en": "👑 GOD MODE",
        "desc_fa": (
            "شورای ۴ مدل مستقل (Claude/GPT/Gemini/DeepSeek + به‌صورت اختیاری NVIDIA) "
            "هرکدام نقش جدا، به‌علاوه یک داور پایتون قطعی؛ فقط وقتی حداقل ۳ مدل هم‌جهت "
            "و میانگین اطمینان بالای ۷۲٪ باشد وارد معامله می‌شود. از کلیدهای API "
            "اختصاصی خودش استفاده می‌کند (بخش GOD MODE در منوی کلیدهای هوش)."
        ),
        "desc_en": (
            "Council of 4 independent models (Claude/GPT/Gemini/DeepSeek, + optional "
            "NVIDIA) each with a distinct role, plus a deterministic Python judge; only "
            "enters when >=3 models agree and average confidence >72%. Uses its own "
            "dedicated API keys (GOD MODE section of the AI keys menu)."
        ),
        # Not actually used -- god_mode is handled by a dedicated code path in
        # AIPipeline.analyze() (see god_mode.py); this key exists only so the
        # STRATEGIES dict lookup never KeyErrors if ever reached by mistake.
        "prompt": BASE_RULES,
    },
}

DEFAULT_STRATEGY = "or_low"

_STAGE1_SYSTEM = """You are the TECHNICAL/CHART stage of a 4-stage trading
analysis pipeline. You receive multi-timeframe technical indicators only
(no fundamentals). Read timeframes together (higher = trend context, lower
= entry timing). Respond with ONLY this JSON, no prose/fences:
{
  "technical_bias": "long" | "short" | "neutral",
  "technical_confidence": <float 0.0-1.0>,
  "key_levels_notes": "<1-2 sentences: trend, momentum, notable levels>"
}
"""

_STAGE2_SYSTEM = """You are the FUNDAMENTAL/POSITIONING stage of a 4-stage
trading analysis pipeline. You receive on-exchange positioning data
(funding rate, open interest, 24h volume/change) for a crypto perp -- this
is the real-time "fundamentals" available, not general news. A strongly
positive funding rate means longs are crowded (contrarian risk of a long
squeeze); strongly negative means shorts are crowded. Respond with ONLY
this JSON, no prose/fences:
{
  "fundamental_bias": "long" | "short" | "neutral",
  "fundamental_confidence": <float 0.0-1.0>,
  "notes": "<1-2 sentences citing funding/OI/volume specifically>"
}
"""

_STAGE3_SYSTEM = """You are the SYNTHESIS stage of a 4-stage trading
analysis pipeline. You receive the outputs of a technical-analysis stage
and a fundamental/positioning-analysis stage for the same symbol. Combine
them: if they agree, the combined signal should be stronger; if they
conflict, say so explicitly and lower the combined strength -- lean toward
the technical read when they disagree, since positioning data is a
secondary/contrarian signal, not primary. Respond with ONLY this JSON, no
prose/fences:
{
  "combined_bias": "long" | "short" | "neutral",
  "signal_strength": <float 0-100>,
  "agreement": "aligned" | "conflicting",
  "synthesis_notes": "<2-3 sentences explaining the combined read>"
}
"""


class AIPipeline:
    def __init__(self, llm_config: dict, reporter=None):
        self._timeout = llm_config.get("request_timeout_seconds", 60)
        self.api_keys = {k: v for k, v in (llm_config.get("api_keys") or {}).items() if v}
        self.strategy = llm_config.get("strategy", DEFAULT_STRATEGY)
        self._clients = {}  # provider -> OpenAI client, built lazily
        self.reporter = reporter
        self.hard_rate_limited = False  # set True once both OpenRouter keys are exhausted
        self._rate_limit_notified = False
        self.call_count = 0  # running count of API calls (for AI cost report)

    def _report_rate_limit(self, detail: str):
        if self.reporter and not self._rate_limit_notified:
            try:
                self.reporter.ai_rate_limited(detail)
            except Exception:
                pass
            self._rate_limit_notified = True

    @classmethod
    def from_settings(cls, base_llm_config: dict, settings: dict, reporter=None) -> "AIPipeline":
        """Rebuilt once per cycle so /strategy (changed live from Telegram)
        takes effect on the very next cycle. API keys themselves are only
        read from config.yaml at process start (like the exchange key) --
        control_bot's "AI keys" menu writes them there and queues a
        restart_engine so a new/changed key is picked up immediately."""
        cfg = dict(base_llm_config)
        cfg["strategy"] = settings.get("strategy", DEFAULT_STRATEGY)
        return cls(cfg, reporter=reporter)

    # ---------------- provider/client resolution ----------------

    def _client_for(self, provider: str):
        if provider not in self.api_keys:
            return None
        if provider not in self._clients:
            self._clients[provider] = OpenAI(
                base_url=PROVIDER_BASE_URLS[provider],
                api_key=self.api_keys[provider],
                timeout=self._timeout,
            )
        return self._clients[provider]

    def _openrouter_keys(self) -> list[str]:
        """Ordered list of OpenRouter slots to try: primary, then backup."""
        keys = []
        if self.api_keys.get("openrouter"):
            keys.append("openrouter")
        if self.api_keys.get("openrouter_backup"):
            keys.append("openrouter_backup")
        return keys

    def _resolve_openrouter(self) -> tuple[str, str] | None:
        """Pick the first available OpenRouter slot (primary, then backup).
        Returns (provider_slot, stage_model_id) or None if none configured."""
        for slot in self._openrouter_keys():
            if slot in self._clients or slot in self.api_keys:
                return slot, None  # model id stays the OpenRouter-style id
        return None

    def _resolve(self, stage_model_id: str) -> tuple[str, str]:
        """Returns (provider, model_id_to_send) for a fixed stage model id.

        For the 'gemini' strategy we prefer the direct Google key, but if it
        is missing (or later fails) we transparently fall back to OpenRouter
        serving the same Google model (google/gemini-2.5-flash). This way the
        user can supply EITHER a Gemini direct key OR an OpenRouter key (or both)
        and the pipeline just works.
        """
        # Gemini direct key takes priority when present.
        if stage_model_id.startswith("gemini") and self._client_for("gemini"):
            return "gemini", stage_model_id

        # Otherwise prefer OpenRouter (one key, reaches every vendor).
        if self._client_for("openrouter"):
            # Rewrite a bare "gemini-2.5-flash" id into the OpenRouter form so
            # the Google model is served through OpenRouter.
            or_id = stage_model_id
            if or_id.startswith("gemini"):
                or_id = "google/" + or_id
            return "openrouter", or_id

        vendor = stage_model_id.split("/", 1)[0]
        mapper = _DIRECT_VENDOR_MAP.get(vendor)
        if mapper:
            provider, direct_model = mapper(stage_model_id)
            if self._client_for(provider):
                return provider, direct_model

        # last resort: any other configured provider, with a substitute model
        for provider in ("openai", "gemini", "nvidia"):
            if self._client_for(provider):
                return provider, _SUBSTITUTE_MODEL[provider]

        raise RuntimeError(
            "No AI provider key configured (openrouter/openai/gemini/nvidia). "
            "Add one from the control bot's 'AI keys' menu."
        )

    def _call_json(self, stage_model_id: str, system_prompt: str, user_content: str,
                   _tried_backup: bool = False, provider_hint: str = None,
                   _fallback_chain: list = None) -> tuple[dict, str]:
        """Returns (parsed_json, model_id_used). On a HARD provider failure
        (auth/404/quota/rate-limit with no backup) it walks STAGE_FALLBACK
        for this stage instead of letting the whole trade die. Retry is done
        per-model inside _call_one; the fallback chain itself is NOT retried
        (otherwise it would re-hit the dead model)."""
        # Build the ordered list of models to try for this call.
        # Primary model FIRST, then fallback chain.
        chain = [stage_model_id]
        if _fallback_chain:
            chain.extend(_fallback_chain)
        last_err = None
        for i, attempt_model in enumerate(chain):
            # Only pass provider_hint for the PRIMARY model (i == 0).
            # Fallback models should resolve their provider automatically.
            hint = provider_hint if i == 0 else None
            try:
                self.call_count += 1
                return self._call_one(attempt_model, system_prompt, user_content,
                                       _tried_backup, hint), attempt_model
            except Exception as e:
                last_err = e
                if self._is_fatal_provider_failure(e, hint):
                    logger.warning(f"Model {attempt_model} hard-failed: {e}; trying fallback")
                    continue  # try next model in the chain
                # transient/unknown error — re-raise, don't burn the chain
                raise
        # all models in the chain failed hard
        raise RuntimeError(f"All models in fallback chain failed: {last_err}")

    def _call_one(self, model_id: str, system_prompt: str, user_content: str,
                  _tried_backup: bool, provider_hint: str | None) -> dict:
        if provider_hint and provider_hint in self.api_keys:
            provider, model = provider_hint, model_id
        elif provider_hint:
            # provider_hint given but no key for it — fall back to OpenRouter or other providers
            logger.warning(f"Provider '{provider_hint}' has no key; falling back to available providers")
            or_slot = self._openrouter_primary_slot(_tried_backup)
            if or_slot:
                provider, model = or_slot, model_id
            else:
                provider, model = self._resolve(model_id)
        else:
            or_slot = self._openrouter_primary_slot(_tried_backup)
            if or_slot:
                provider, model = or_slot, model_id
            else:
                provider, model = self._resolve(model_id)
        client = self._client_for(provider)
        # or_low (+ any persona under it) and nvidia use a 150-token cap
        # (free-tier friendly); or_high / gemini use the full 300.
        _mode = self.strategy.split("__", 1)[0] if "__" in self.strategy else self.strategy
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.2,
                max_tokens=150 if _mode in ("or_low", "nvidia") else 300,
            )
        except Exception as e:
            if (not provider_hint) and _is_rate_limit(e) and not _tried_backup and self._has_backup_openrouter():
                logger.warning(f"Rate limit on {provider}; retrying on backup OpenRouter key")
                return self._call_one(model_id, system_prompt, user_content, True, provider_hint)
            raise
        raw = response.choices[0].message.content
        cleaned = raw.strip()
        # Extract JSON from response (handle markdown fences, extra text, etc.)
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
        # Find first { and last } to extract JSON object
        first_brace = cleaned.find("{")
        last_brace = cleaned.rfind("}")
        if first_brace >= 0 and last_brace > first_brace:
            cleaned = cleaned[first_brace:last_brace+1]
        return json.loads(cleaned)

    def _openrouter_primary_slot(self, tried_backup: bool) -> str | None:
        """Return the OpenRouter slot to use for this call. If backup already
        tried, return None so the caller falls through to direct providers."""
        slots = self._openrouter_keys()
        if not slots:
            return None
        if tried_backup:
            return slots[-1] if len(slots) > 1 else None
        return slots[0]

    def _has_backup_openrouter(self) -> bool:
        return bool(self.api_keys.get("openrouter_backup"))

    def _is_fatal_provider_failure(self, err: Exception, provider_hint: str | None) -> bool:
        """True when the provider this strategy depends on is dead (auth fail,
        quota/credit exhausted, not found, or rate-limited with no backup).
        A transient network error should NOT trip this."""
        msg = str(err).lower()
        # A model being removed by the vendor (404 / no endpoints) must ALWAYS
        # fall through to the next model in the fallback chain, regardless of
        # whether a backup OpenRouter key exists — a backup key won't bring a
        # dead model back. Auth/credit failures (401/402/403) for the whole
        # account do depend on the backup key.
        model_dead_markers = ("404", "no endpoints", "not found", "notfound",
                              "notfounderror", "not found error")
        if any(m in msg for m in model_dead_markers):
            return True
        account_fatal_markers = (
            "401", "402", "403", "429",
            "unauthorized", "invalid api key", "incorrect api key",
            "quota", "credit", "exceeded", "authentication",
            "permission", "forbidden",
        )
        if any(m in msg for m in account_fatal_markers):
            if provider_hint:
                return True
            return not self._has_backup_openrouter()
        return False

    # ---------------- the 4-stage pipeline ----------------

    def analyze(self, symbol: str, indicator_summary: str, fundamental_summary: str = "") -> dict:
        """Runs all 4 stages and returns a validated final decision dict, or
        a safe 'hold' if the decision stage can't be reached at all."""
        if self.strategy == "god_mode":
            return self._analyze_god_mode(symbol, indicator_summary, fundamental_summary)

        used = []
        # Pick models for this strategy: gemini strategy -> its own models
        # (Gemini-only, never touches other providers); everything else ->
        # the shared OpenRouter-backed STAGE_MODELS.
        strategy_models = models_for_strategy(self.strategy)
        spec = STRATEGY_MODELS.get(self.strategy)
        provider_hint = spec["provider"] if spec else None

        # Stage 1: chart/technical
        try:
            s1, m1 = self._call_json(
                strategy_models["chart"], _STAGE1_SYSTEM,
                f"Symbol: {symbol}\n\nIndicators:\n{indicator_summary}",
                provider_hint=provider_hint,
                _fallback_chain=STAGE_FALLBACK["chart"],
            )
            used.append(f"chart:{m1}")
        except Exception as e:
            logger.warning(f"Stage1 (chart) failed for {symbol}: {e}")
            s1 = {"technical_bias": "neutral", "technical_confidence": 0.0,
                  "key_levels_notes": "technical stage unavailable"}

        # Stage 2: fundamental/positioning
        try:
            s2, m2 = self._call_json(
                strategy_models["fundamental"], _STAGE2_SYSTEM,
                f"Symbol: {symbol}\n\nMarket context:\n{fundamental_summary or 'unavailable'}",
                provider_hint=provider_hint,
                _fallback_chain=STAGE_FALLBACK["fundamental"],
            )
            used.append(f"fund:{m2}")
        except Exception as e:
            logger.warning(f"Stage2 (fundamental) failed for {symbol}: {e}")
            s2 = {"fundamental_bias": "neutral", "fundamental_confidence": 0.0,
                  "notes": "fundamental stage unavailable"}

        # Stage 3: synthesis
        try:
            s3, m3 = self._call_json(
                strategy_models["synthesis"], _STAGE3_SYSTEM,
                f"Symbol: {symbol}\n\nTechnical stage output:\n{json.dumps(s1)}\n\n"
                f"Fundamental stage output:\n{json.dumps(s2)}",
                provider_hint=provider_hint,
                _fallback_chain=STAGE_FALLBACK["synthesis"],
            )
            used.append(f"synth:{m3}")
        except Exception as e:
            logger.warning(f"Stage3 (synthesis) failed for {symbol}: {e}")
            # fall back to the technical read alone
            s3 = {
                "combined_bias": s1.get("technical_bias", "neutral"),
                "signal_strength": round(float(s1.get("technical_confidence", 0)) * 100, 1),
                "agreement": "unknown", "synthesis_notes": "synthesis stage unavailable, using technical read only",
            }

        # Stage 4: final decision (strategy-persona specific).
        # strategy may be "or_low__scalper" / "or_high__god_mode" (token mode
        # + persona) or a plain provider key ("nvidia"/"gemini"). Extract the
        # persona suffix if present; otherwise fall back to "balanced".
        _persona_key = self.strategy
        if "__" in _persona_key:
            _persona_key = _persona_key.split("__", 1)[1]
        if _persona_key not in STRATEGIES:
            _persona_key = "balanced"
        strategy_prompt = STRATEGIES.get(_persona_key, STRATEGIES["balanced"])["prompt"]
        decision_input = (
            f"Symbol: {symbol}\n\n"
            f"You are given the OUTPUT OF A PRIOR SYNTHESIS STAGE that already combined "
            f"technical and fundamental/positioning analysis -- treat it as your "
            f"'indicators' input:\n"
            f"combined_bias={s3.get('combined_bias')} "
            f"signal_strength={s3.get('signal_strength')}/100 "
            f"agreement={s3.get('agreement')}\n"
            f"synthesis_notes: {s3.get('synthesis_notes')}\n\n"
            f"Supporting detail -- technical stage: {s1.get('key_levels_notes')}\n"
            f"Supporting detail -- fundamental stage: {s2.get('notes')}\n"
        )
        try:
            raw, m4 = self._call_json(strategy_models["decision"], strategy_prompt, decision_input,
                                     provider_hint=provider_hint,
                                     _fallback_chain=STAGE_FALLBACK["decision"])
            used.append(f"decision:{m4}")
            parsed = self._validate(raw)
            parsed["_model_used"] = "4-stage pipeline (" + " → ".join(used) + ")"
            return parsed
        except Exception as e:
            logger.warning(f"Stage4 (decision) failed for {symbol}: {e}")
            # Hard rate-limit / hard-failure on the decision stage -> stop the bot.
            # For the gemini strategy this means the Gemini key died; for others
            # it means OpenRouter (primary+backup) is exhausted.
            if self._is_fatal_provider_failure(e, provider_hint):
                self.hard_rate_limited = True
                detail = (
                    f"The required provider for strategy '{self.strategy}' stopped "
                    f"working. Trading halted — fix the key from the AI keys menu and "
                    f"restart the engine."
                )
                self._report_rate_limit(detail)

        return {
            "signal": "hold",
            "confidence": 0.0,
            "suggested_capital_pct": 0.0,
            "suggested_stop_loss_pct": 1.5,
            "suggested_take_profit_pct": 3.0,
            "reasoning": "AI pipeline decision stage failed; defaulting to hold.",
            "_model_used": "4-stage pipeline (decision stage failed: " + ", ".join(used) + ")",
        }

    def _analyze_god_mode(self, symbol: str, indicator_summary: str, fundamental_summary: str) -> dict:
        """GOD MODE is a self-contained council-of-models + deterministic-judge
        strategy (see god_mode.py) with its own dedicated API keys
        (llm.api_keys.godmode_openrouter/godmode_gemini/godmode_nvidia) --
        it deliberately does NOT reuse the main pipeline's keys/clients so
        it never competes with the everyday strategies for quota."""
        analyzer = GodModeAnalyzer(self.api_keys, timeout=self._timeout, reporter=self.reporter)
        result = analyzer.analyze(symbol, indicator_summary, fundamental_summary)
        self.hard_rate_limited = analyzer.hard_rate_limited
        parsed = self._validate(result)
        parsed["_model_used"] = result.get("_model_used", "GOD MODE")
        return parsed

    @staticmethod
    def _validate(data: dict) -> dict:
        signal = data.get("signal")
        if signal not in ("long", "short", "hold"):
            raise ValueError(f"invalid signal: {signal}")

        confidence = float(data.get("confidence", 0))
        confidence = max(0.0, min(1.0, confidence))

        return {
            "signal": signal,
            "confidence": confidence,
            "suggested_capital_pct": max(0.0, min(100.0, float(data.get("suggested_capital_pct", 50)))),
            "suggested_stop_loss_pct": abs(float(data.get("suggested_stop_loss_pct", 1.5))),
            "suggested_take_profit_pct": abs(float(data.get("suggested_take_profit_pct", 3.0))),
            "reasoning": str(data.get("reasoning", ""))[:500],
        }
