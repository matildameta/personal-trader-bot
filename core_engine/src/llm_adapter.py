"""
Multi-model, multi-strategy LLM adapter.

Works with ANY provider that exposes an OpenAI-compatible chat completions
endpoint. By default this points at OpenRouter, which lets a single API
key reach dozens of underlying models (OpenAI, Anthropic, DeepSeek,
Google, Meta, Qwen, Mistral, ...) by model id alone -- so "switch model"
and "add a new model" are just a model-id/key change, live from Telegram,
no redeploy needed.

STRATEGIES defines several distinct analysis personas (conservative,
aggressive, scalper, ...). Each has its own system prompt tuned for a
different risk appetite / holding period, so the same indicators can be
read very differently depending on which persona is active. The active
strategy and active model are both stored in the shared DB and can be
changed anytime from control_bot.
"""
import json
import logging
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_fixed

logger = logging.getLogger("llm_adapter")

# ---------------------------------------------------------------------------
# Curated catalog of strong models reachable via OpenRouter with one API key.
# Shown in the Telegram /models menu with a short description each. Any
# other OpenRouter-style model id can still be set manually with /setmodel.
# ---------------------------------------------------------------------------
MODEL_CATALOG = [
    {
        "id": "deepseek/deepseek-chat",
        "label": "DeepSeek Chat",
        "desc_fa": "قوی در تحلیل عددی، سریع و ارزان — گزینه پیش‌فرض خوب",
        "desc_en": "Strong at numeric/technical reasoning, fast and cheap",
    },
    {
        "id": "openai/gpt-4o",
        "label": "GPT-4o",
        "desc_fa": "استدلال دقیق و چندلایه، مناسب تحلیل پیچیده‌تر",
        "desc_en": "Precise, multi-step reasoning, good for complex setups",
    },
    {
        "id": "openai/gpt-4o-mini",
        "label": "GPT-4o mini",
        "desc_fa": "سریع و ارزان، مناسب چرخه‌های کوتاه و فرکانس بالا",
        "desc_en": "Fast and cheap, good for short cycles / high frequency",
    },
    {
        "id": "anthropic/claude-3.5-sonnet",
        "label": "Claude 3.5 Sonnet",
        "desc_fa": "تحلیل عمیق و محتاطانه، خوب در توضیح دلیل تصمیم",
        "desc_en": "Deep, careful analysis; explains reasoning well",
    },
    {
        "id": "google/gemini-2.0-flash-exp",
        "label": "Gemini 2.0 Flash",
        "desc_fa": "بسیار سریع، مناسب تحلیل هم‌زمان چند نماد",
        "desc_en": "Very fast, good for scanning many symbols at once",
    },
    {
        "id": "qwen/qwen-2.5-72b-instruct",
        "label": "Qwen 2.5 72B",
        "desc_fa": "قوی در تشخیص الگوهای عددی، هزینه پایین",
        "desc_en": "Strong at numeric pattern recognition, low cost",
    },
    {
        "id": "meta-llama/llama-3.1-70b-instruct",
        "label": "Llama 3.1 70B",
        "desc_fa": "متن‌باز، پایدار، هزینه بسیار پایین",
        "desc_en": "Open-weight, stable, very low cost",
    },
    {
        "id": "mistralai/mistral-large",
        "label": "Mistral Large",
        "desc_fa": "متعادل بین سرعت و دقت",
        "desc_en": "Balanced speed/accuracy",
    },
]

# Note: OpenRouter's catalog changes over time -- check openrouter.ai/models
# for the current list/pricing. Any valid OpenRouter model id works even if
# not listed above (use /setmodel <id>).

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
    "god_mode": {
        "label_fa": "👑 گاد مود (God Mode)",
        "label_en": "👑 God Mode",
        "desc_fa": "شورای چندمدلی (Claude/GPT/Gemini/DeepSeek) با داور قطعی؛ بالاترین کیفیت تحلیل.",
        "desc_en": "Multi-model council (Claude/GPT/Gemini/DeepSeek) with decisive judge; highest analysis quality.",
        "prompt": BASE_RULES + """
STRATEGY PERSONA: God Mode. You are part of a multi-model council. Provide
your most rigorous, well-reasoned directional view. Weigh all timeframes,
cross-check fundamental positioning against technical structure, and only
output "hold" when there is genuine contradiction across models. Aim for
high-conviction, well-supported signals with clear reasoning.
""",
    },
}

DEFAULT_STRATEGY = "balanced"


class LLMAdapter:
    def __init__(self, llm_config: dict):
        self._base_url = llm_config["base_url"]
        self._timeout = llm_config.get("request_timeout_seconds", 60)
        self.api_key = llm_config["api_key"]
        self.primary_model = llm_config["model"]
        self.fallback_model = llm_config.get("fallback_model")
        self.strategy = llm_config.get("strategy", DEFAULT_STRATEGY)
        self.client = OpenAI(base_url=self._base_url, api_key=self.api_key or "unset", timeout=self._timeout)

    @classmethod
    def from_settings(cls, base_llm_config: dict, settings: dict) -> "LLMAdapter":
        """Build (or rebuild) an adapter reflecting whatever is currently
        stored in the shared DB -- called once per cycle so /setapikey,
        /setmodel and /strategy take effect on the very next cycle with no
        restart."""
        cfg = dict(base_llm_config)
        if settings.get("llm_api_key"):
            cfg["api_key"] = settings["llm_api_key"]
        if settings.get("active_model"):
            cfg["model"] = settings["active_model"]
        if settings.get("fallback_model"):
            cfg["fallback_model"] = settings["fallback_model"]
        cfg["strategy"] = settings.get("strategy", DEFAULT_STRATEGY)
        return cls(cfg)

    def analyze(self, symbol: str, indicator_summary: str) -> dict:
        """Returns a validated dict signal, or a safe 'hold' on total failure."""
        system_prompt = STRATEGIES.get(self.strategy, STRATEGIES[DEFAULT_STRATEGY])["prompt"]
        for model, label in (
            (self.primary_model, "primary"),
            (self.fallback_model, "fallback"),
        ):
            if not model:
                continue
            try:
                raw = self._call(model, system_prompt, symbol, indicator_summary)
                parsed = self._validate(raw)
                parsed["_model_used"] = f"{model} ({label})"
                return parsed
            except Exception as e:
                logger.warning(f"LLM call failed on {label} model ({model}): {e}")
                continue

        return {
            "signal": "hold",
            "confidence": 0.0,
            "suggested_capital_pct": 0.0,
            "suggested_stop_loss_pct": 1.5,
            "suggested_take_profit_pct": 3.0,
            "reasoning": "All LLM providers failed; defaulting to hold.",
            "_model_used": "none",
        }

    @retry(stop=stop_after_attempt(2), wait=wait_fixed(2))
    def _call(self, model: str, system_prompt: str, symbol: str, indicator_summary: str) -> str:
        response = self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"Symbol: {symbol}\n\nIndicators:\n{indicator_summary}",
                },
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content

    @staticmethod
    def _validate(raw: str) -> dict:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
        data = json.loads(cleaned)

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
