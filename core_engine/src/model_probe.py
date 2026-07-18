"""Standalone probe: runs a real sample analysis through every strategy's
pipeline and reports signal/confidence. Placed INSIDE core_engine/src so
ai_pipeline's relative imports (e.g. `from .god_mode import ...`) resolve
correctly as part of this package. control_bot calls this via subprocess
or by adding core_engine/src to sys.path and importing `model_probe`."""

import os
import sys
import yaml

# Make sibling imports (ai_pipeline -> .god_mode) work
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from src.ai_pipeline import AIPipeline  # noqa: E402


def _load_engine_config():
    cfg_path = os.path.join(_HERE, "..", "config.yaml")
    with open(cfg_path) as f:
        return yaml.safe_load(f), None


def run_sample_test(lang: str = "en") -> str:
    cfg, _ = _load_engine_config()
    llm_cfg = dict(cfg.get("llm", {}) or {})
    sample_ind = ("RSI(5m)=28 (oversold), EMA20>EMA50 on 1h (uptrend), "
                  "BB lower band touched on 5m, ATR(14)=1.2%")
    sample_fund = "Funding positive, OI rising"
    strategies = ["or_low__balanced", "or_high__god_mode", "nvidia", "gemini"]
    title = ("🧪 تست واقعی مدل‌ها (نمونه تحلیل):" if lang == "fa"
             else "🧪 Live model test (sample analysis):")
    lines = [title, "━━━━━━━━━━━━━━"]
    all_ok = True
    for strat in strategies:
        llm = dict(llm_cfg)
        llm["strategy"] = strat
        try:
            pipe = AIPipeline(llm)
            d = pipe.analyze("BTC", sample_ind, sample_fund)
            sig = d.get("signal", "?")
            conf = d.get("confidence", 0)
            cap = pipe.call_count
            ok_mark = "🟢" if sig in ("long", "short", "hold") else "🔴"
            if ok_mark == "🔴":
                all_ok = False
            lines.append(f"{ok_mark} {strat}: signal={sig} conf={conf:.2f} (calls={cap})")
        except Exception as e:
            all_ok = False
            lines.append(f"🔴 {strat}: ERROR {str(e)[:60]}")
    lines.append("")
    lines.append(("✅ همه استراتژی‌ها پاسخ دادند" if lang == "fa" else "✅ All strategies responded")
                 if all_ok else
                 ("⚠️ برخی استراتژی‌ها خطا داشتند" if lang == "fa" else "⚠️ Some strategies errored"))
    return "\n".join(lines)


if __name__ == "__main__":
    print(run_sample_test("fa"))
