"""
GOD MODE -- optional multi-model "council + judge" strategy add-on.

This is a SEPARATE, self-contained analysis path, not part of the fixed
4-stage pipeline in ai_pipeline.py. It is only reached when /strategy is
set to "god_mode". It deliberately uses ITS OWN dedicated API keys
(config.yaml -> llm.api_keys.godmode_openrouter / godmode_gemini /
godmode_nvidia), separate from the main pipeline's openrouter/openai/gemini
keys, so this strategy never competes for quota with the everyday
strategies (or vice versa).

Four independent analysts run every cycle, each a fixed model with a
narrow, non-overlapping role:

  1. CLAUDE   (godmode_openrouter) -- Master Technical Analyst. Reads the
              same multi-timeframe indicators as everything else in this
              project and proposes a direction + entry zone + SL/TP.
  2. GPT      (godmode_openrouter) -- Trading Strategist / risk reviewer.
              Does NOT redo technical analysis -- it reviews Claude's
              proposal for trade quality and risk/reward and either
              approves, asks to modify, or rejects it.
  3. GEMINI   (godmode_gemini)     -- Market Sentiment + News Hunter. Reads
              on-exchange positioning (funding/OI, from fundamentals.py)
              plus the live crypto Fear & Greed Index (fetched here, no
              key needed) as its "fundamental mood" input. Plain chat
              completions have no live web/Twitter browsing, so that part
              of the original brief is intentionally not simulated with
              invented "news".
  4. DEEPSEEK (godmode_openrouter) -- Scalping Pattern Detector. Looks for
              short-term pattern/momentum setups and gives a historical-
              similarity style read.

  5. NVIDIA (godmode_nvidia, OPTIONAL) -- Independent Cross-Check analyst.
              Not in the original 4-model brief; only runs if a
              godmode_nvidia key is configured (NVIDIA's NIM API,
              https://build.nvidia.com). Added because more independent
              votes only strengthen the quorum below, never weaken it.

GOD MODE JUDGE -- the final decision is NOT another AI call. It is plain,
deterministic Python (same philosophy as risk_engine.py: the AI proposes,
code decides):

  - Every analyst's output is reduced to (direction, confidence 0-100).
  - Whichever direction (long/short) has more votes is the candidate side;
    "hold"/neutral votes are excluded from both the count and the average.
  - ENTRY requires >= 3 analysts agreeing on that side AND their average
    confidence >= 72%.
  - STRONG ENTRY is the same rule with ALL active analysts agreeing AND
    average confidence >= 85% -- it does not unlock a separate code path,
    just the top position-size tier and a "[STRONG ENTRY]" tag in the
    reasoning shown in Telegram reports.
  - Position size tier (suggested_capital_pct, itself still capped by
    /settradepct and deterministically sized by risk_engine.py exactly
    like every other strategy -- GOD MODE cannot bypass max_leverage or
    the kill-switch):
      70-75% avg confidence  -> 25%
      75-85% avg confidence  -> 50%
      85%+   avg confidence  -> 85% (top of the 75-100% band the brief
                                      asked for; the exact ceiling is still
                                      /settradepct + max_leverage, enforced
                                      the same way as every other strategy)
  - Anything below the 3-agree/72% quorum -> "hold", no trade this cycle.

Entry price / stop-loss / take-profit come from Claude's technical read
(the only analyst asked for concrete levels), falling back to safe
defaults if that call failed.
"""
import json
import logging

import requests
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_fixed

logger = logging.getLogger("god_mode")

PROVIDER_BASE_URLS = {
    "godmode_openrouter": "https://openrouter.ai/api/v1",
    "godmode_gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "godmode_nvidia": "https://integrate.api.nvidia.com/v1",
}

# Fixed model per analyst. Swap the "model" string for any other
# OpenRouter/NVIDIA NIM catalog id if you want to tune this later --
# nothing else in this file needs to change.
ANALYSTS = {
    "claude": {
        "provider": "godmode_openrouter",
        "model": "anthropic/claude-3-haiku",   # was claude-3.5-sonnet (removed from OpenRouter 404)
        "label": "Claude (Master Technical Analyst)",
    },
    "gpt": {
        "provider": "godmode_openrouter",
        "model": "openai/gpt-4o",
        "label": "GPT (Trading Strategist)",
    },
    "gemini": {
        "provider": "godmode_gemini",
        "model": "gemini-2.5-flash",           # was gemini-2.0-flash-exp (removed from OpenRouter 404)
        "label": "Gemini (Sentiment + News)",
    },
    "deepseek": {
        "provider": "godmode_openrouter",
        "model": "deepseek/deepseek-chat",
        "label": "DeepSeek (Scalping Patterns)",
    },
    "nvidia": {
        "provider": "godmode_nvidia",
        "model": "nvidia/nemotron-3-ultra-550b-a55b",
        "label": "NVIDIA Nemotron (Cross-Check)",
    },
}

REQUIRED_ANALYSTS = ("claude", "gpt", "deepseek")  # gemini dropped: its key is broken (404/429)
OPTIONAL_ANALYSTS = ("nvidia", "gemini")            # nvidia=nemotron (works); gemini optional (key may fail)

MIN_AGREE = 3
MIN_AVG_CONFIDENCE = 72.0

_RATE_LIMIT_MARKERS = (
    "401", "402", "403", "404", "429",
    "unauthorized", "invalid api key", "incorrect api key",
    "quota", "credit", "exceeded", "not found", "no endpoints",
    "authentication", "permission", "forbidden",
    "rate limit", "rate_limit", "ratelimit", "too many requests",
)


def _is_fatal(err: Exception) -> bool:
    msg = str(err).lower()
    return any(m in msg for m in _RATE_LIMIT_MARKERS)


def fetch_fear_greed_index() -> str:
    """Free, no-key public endpoint -- real-time crypto Fear & Greed Index,
    used as Gemini's concrete 'market mood' data point since a plain chat
    completion has no live news/Twitter browsing of its own."""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        r.raise_for_status()
        d = r.json()["data"][0]
        return f"{d.get('value')} ({d.get('value_classification')})"
    except Exception as e:
        logger.warning(f"Fear & Greed Index fetch failed: {e}")
        return "unavailable"


_CLAUDE_SYSTEM = """You are the MASTER TECHNICAL ANALYST in a multi-AI crypto
futures trading council ("GOD MODE"). You analyze price action ONLY --
never news or sentiment, another analyst covers that. You receive
multi-timeframe technical indicators (candles/RSI/MACD/EMA/volume/ADX/
Bollinger Bands already computed). Judge: the overall trend, whether any
breakout is valid, whether entry is already late, the best entry zone, and
a sensible stop-loss. Respond with ONLY this JSON, no prose/fences:
{
  "direction": "long" | "short" | "neutral",
  "confidence": <float 0-100>,
  "entry_zone_low": <float>,
  "entry_zone_high": <float>,
  "stop_loss_pct": <float, e.g. 1.5>,
  "take_profit_pct": <float, e.g. 3.0>,
  "reason": "<2-3 concise sentences citing specific indicators/levels>"
}
"""

_GPT_SYSTEM = """You are the TRADING STRATEGIST in a multi-AI crypto futures
trading council ("GOD MODE"). You do NOT redo technical analysis -- you
review the technical analyst's proposal for trade quality and risk
management: is the risk/reward ratio reasonable, is now a good time for a
futures position given volatility, how likely is the move to continue.
Respond with ONLY this JSON, no prose/fences:
{
  "trade_quality": "A" | "B" | "C",
  "confidence": <float 0-100>,
  "recommendation": "APPROVE" | "MODIFY" | "REJECT",
  "reason": "<1-2 concise sentences>"
}
"""

_GEMINI_SYSTEM = """You are the MARKET SENTIMENT + NEWS analyst in a multi-AI
crypto futures trading council ("GOD MODE"). You receive on-exchange
positioning data (funding rate / open interest / 24h volume) and the
current crypto Fear & Greed Index -- treat these as your available
fundamental/sentiment signal (you do not have live news or social-media
browsing access, so do not invent specific news events). Judge whether
there is a fundamental/sentiment reason behind the current move. Respond
with ONLY this JSON, no prose/fences:
{
  "market_mood": "Bullish" | "Bearish" | "Neutral",
  "impact": "Low" | "Medium" | "High",
  "confidence": <float 0-100>,
  "reason": "<1-2 concise sentences citing the specific data given>"
}
"""

_DEEPSEEK_SYSTEM = """You are the SCALPING PATTERN DETECTOR in a multi-AI
crypto futures trading council ("GOD MODE"). You look at short-term
technical structure (lowest timeframe especially) for pattern setups and
how similar setups have typically resolved. Respond with ONLY this JSON,
no prose/fences:
{
  "historical_win_rate_pct": <float 0-100, your estimate>,
  "prediction": "long" | "short",
  "confidence": <float 0-100>,
  "reason": "<1-2 concise sentences>"
}
"""

_NVIDIA_SYSTEM = """You are an INDEPENDENT CROSS-CHECK analyst in a multi-AI
crypto futures trading council ("GOD MODE"). You look at the same
multi-timeframe indicators as the other analysts and give your own,
independent short-term directional read -- do not assume any other
analyst's conclusion. Respond with ONLY this JSON, no prose/fences:
{
  "prediction": "long" | "short",
  "confidence": <float 0-100>,
  "reason": "<1-2 concise sentences>"
}
"""


class GodModeAnalyzer:
    def __init__(self, api_keys: dict, timeout: int = 60, reporter=None):
        self.api_keys = api_keys
        self.timeout = timeout
        self.reporter = reporter
        self._clients = {}
        self.hard_rate_limited = False
        self._rate_limit_notified = False

    def _client_for(self, provider: str):
        key = self.api_keys.get(provider)
        if not key:
            return None
        if provider not in self._clients:
            self._clients[provider] = OpenAI(
                base_url=PROVIDER_BASE_URLS[provider], api_key=key, timeout=self.timeout,
            )
        return self._clients[provider]

    def active_analysts(self) -> list:
        names = [n for n in REQUIRED_ANALYSTS if self.api_keys.get(ANALYSTS[n]["provider"])]
        names += [n for n in OPTIONAL_ANALYSTS if self.api_keys.get(ANALYSTS[n]["provider"])]
        return names

    @retry(stop=stop_after_attempt(2), wait=wait_fixed(2))
    def _call(self, name: str, system_prompt: str, user_content: str) -> dict:
        spec = ANALYSTS[name]
        client = self._client_for(spec["provider"])
        if client is None:
            raise RuntimeError(f"no key configured for {spec['provider']}")
        response = client.chat.completions.create(
                    model=spec["model"],
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0.2,
                    max_tokens=150 if spec["provider"] == "godmode_nvidia" else 300,
                )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw.split("\n", 1)[-1] if "\n" in raw else raw
        return json.loads(raw)

    def _report_rate_limit(self, detail: str):
        if self.reporter and not self._rate_limit_notified:
            try:
                self.reporter.ai_rate_limited(detail)
            except Exception:
                pass
            self._rate_limit_notified = True

    # ---------------- the council + judge ----------------

    def analyze(self, symbol: str, indicator_summary: str, fundamental_summary: str = "") -> dict:
        active = self.active_analysts()
        if not any(a in active for a in REQUIRED_ANALYSTS):
            self.hard_rate_limited = True
            detail = (
                "GOD MODE has no configured keys (llm.api_keys.godmode_openrouter / "
                "godmode_gemini) -- add them from the AI keys menu and restart the engine."
            )
            self._report_rate_limit(detail)
            return self._hold(detail)

        results = {}
        errors = []

        try:
            results["claude"] = self._call(
                "claude", _CLAUDE_SYSTEM,
                f"Symbol: {symbol}\n\nMulti-timeframe indicators:\n{indicator_summary}",
            )
        except Exception as e:
            logger.warning(f"GOD MODE claude analyst failed for {symbol}: {e}")
            errors.append(f"claude: {e}")

        try:
            claude_view = json.dumps(results.get("claude", {"note": "technical analyst unavailable"}))
            results["gpt"] = self._call(
                "gpt", _GPT_SYSTEM,
                f"Symbol: {symbol}\n\nTechnical analyst's proposal:\n{claude_view}\n\n"
                f"Market/positioning context:\n{fundamental_summary or 'unavailable'}",
            )
        except Exception as e:
            logger.warning(f"GOD MODE gpt analyst failed for {symbol}: {e}")
            errors.append(f"gpt: {e}")

        fng = fetch_fear_greed_index()
        try:
            results["gemini"] = self._call(
                "gemini", _GEMINI_SYSTEM,
                f"Symbol: {symbol}\n\nOn-exchange positioning:\n{fundamental_summary or 'unavailable'}\n\n"
                f"Crypto Fear & Greed Index: {fng}",
            )
        except Exception as e:
            logger.warning(f"GOD MODE gemini analyst failed for {symbol}: {e}")
            errors.append(f"gemini: {e}")

        try:
            results["deepseek"] = self._call(
                "deepseek", _DEEPSEEK_SYSTEM,
                f"Symbol: {symbol}\n\nMulti-timeframe indicators:\n{indicator_summary}",
            )
        except Exception as e:
            logger.warning(f"GOD MODE deepseek analyst failed for {symbol}: {e}")
            errors.append(f"deepseek: {e}")

        if "nvidia" in active:
            try:
                results["nvidia"] = self._call(
                    "nvidia", _NVIDIA_SYSTEM,
                    f"Symbol: {symbol}\n\nMulti-timeframe indicators:\n{indicator_summary}",
                )
            except Exception as e:
                logger.warning(f"GOD MODE nvidia analyst failed for {symbol}: {e}")
                errors.append(f"nvidia: {e}")

        if not results:
            if all(_is_fatal(Exception(e)) for e in errors) if errors else False:
                pass  # handled below uniformly
            fatal = errors and all(any(m in e.lower() for m in _RATE_LIMIT_MARKERS) for e in errors)
            if fatal:
                self.hard_rate_limited = True
                self._report_rate_limit(
                    "GOD MODE: every configured analyst provider failed (bad/expired key or "
                    "rate-limited). Trading halted -- fix the key(s) and restart the engine."
                )
            return self._hold("all GOD MODE analysts failed this cycle: " + "; ".join(errors))

        votes = self._extract_votes(results)
        directional = [v for v in votes if v[1] in ("long", "short")]
        if len(directional) < 2:
            return self._hold(
                "not enough GOD MODE analysts returned a directional read this cycle "
                f"({len(directional)}/{len(votes)}); errors: " + "; ".join(errors)
            )

        decision = self._judge(votes, results, total_analysts=len(results))
        decision["_model_used"] = "GOD MODE (" + "+".join(sorted(results.keys())) + ")"
        return decision

    @staticmethod
    def _extract_votes(results: dict) -> list:
        """Returns [(analyst_name, 'long'|'short'|'hold', confidence_0_100), ...]"""
        votes = []

        if "claude" in results:
            d = results["claude"]
            direction = d.get("direction") if d.get("direction") in ("long", "short") else "hold"
            votes.append(("claude", direction, float(d.get("confidence", 0) or 0)))

        if "gpt" in results:
            d = results["gpt"]
            rec = d.get("recommendation", "REJECT")
            claude_dir = results.get("claude", {}).get("direction")
            direction = claude_dir if rec in ("APPROVE", "MODIFY") and claude_dir in ("long", "short") else "hold"
            votes.append(("gpt", direction, float(d.get("confidence", 0) or 0)))

        if "gemini" in results:
            d = results["gemini"]
            mood = d.get("market_mood", "Neutral")
            direction = {"Bullish": "long", "Bearish": "short"}.get(mood, "hold")
            votes.append(("gemini", direction, float(d.get("confidence", 0) or 0)))

        if "deepseek" in results:
            d = results["deepseek"]
            direction = d.get("prediction") if d.get("prediction") in ("long", "short") else "hold"
            votes.append(("deepseek", direction, float(d.get("confidence", 0) or 0)))

        if "nvidia" in results:
            d = results["nvidia"]
            direction = d.get("prediction") if d.get("prediction") in ("long", "short") else "hold"
            votes.append(("nvidia", direction, float(d.get("confidence", 0) or 0)))

        return votes

    @staticmethod
    def _judge(votes: list, results: dict, total_analysts: int) -> dict:
        longs = [(n, c) for n, dirn, c in votes if dirn == "long"]
        shorts = [(n, c) for n, dirn, c in votes if dirn == "short"]

        if not longs and not shorts:
            return GodModeAnalyzer._hold("no analyst gave a directional read")

        side, agreeing = ("long", longs) if len(longs) >= len(shorts) else ("short", shorts)
        agree_count = len(agreeing)
        avg_conf = sum(c for _, c in agreeing) / agree_count if agreeing else 0.0

        vote_summary = " ".join(
            f"{n}={dirn.upper()}({c:.0f}%)" for n, dirn, c in votes
        )

        if agree_count < MIN_AGREE or avg_conf < MIN_AVG_CONFIDENCE:
            return GodModeAnalyzer._hold(
                f"GOD MODE: only {agree_count}/{len(votes)} analysts agree on {side.upper()} "
                f"(avg confidence {avg_conf:.0f}%), below the {MIN_AGREE}-agree/"
                f"{MIN_AVG_CONFIDENCE:.0f}% quorum -- no trade. [{vote_summary}]"
            )

        strong = agree_count == total_analysts and avg_conf >= 85.0

        if avg_conf >= 85.0:
            capital_pct = 85.0
        elif avg_conf >= 75.0:
            capital_pct = 50.0
        else:
            capital_pct = 25.0

        claude = results.get("claude", {})
        sl_pct = abs(float(claude.get("stop_loss_pct") or 1.5)) or 1.5
        tp_pct = abs(float(claude.get("take_profit_pct") or 3.0)) or 3.0

        tag = "[STRONG ENTRY] " if strong else "[ENTRY] "
        reasoning = (
            f"{tag}{agree_count}/{len(votes)} analysts agree {side.upper()}, "
            f"avg confidence {avg_conf:.0f}%. [{vote_summary}]"
        )

        return {
            "signal": side,
            "confidence": max(0.0, min(1.0, avg_conf / 100.0)),
            "suggested_capital_pct": capital_pct,
            "suggested_stop_loss_pct": sl_pct,
            "suggested_take_profit_pct": tp_pct,
            "reasoning": reasoning[:500],
        }

    @staticmethod
    def _hold(reason: str) -> dict:
        return {
            "signal": "hold",
            "confidence": 0.0,
            "suggested_capital_pct": 0.0,
            "suggested_stop_loss_pct": 1.5,
            "suggested_take_profit_pct": 3.0,
            "reasoning": str(reason)[:500],
        }
