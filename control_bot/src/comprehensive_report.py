"""Comprehensive account report pulled live from the Hyperliquid API.

Everything here is read-only and needs only the account address (plus the
network). It aggregates account state, open positions, open orders, the full
fill history (fees, volume, realized P&L), funding payments, and deposit/
withdrawal ledger into one rich Telegram-friendly report.

Nothing here blocks a trading cycle — it's called on demand from the control
bot's "📊 گزارش جامع" button.
"""
import time
from collections import defaultdict
from hyperliquid.info import Info
from hyperliquid.utils import constants


def _api_url(network: str) -> str:
    return constants.TESTNET_API_URL if network == "testnet" else constants.MAINNET_API_URL


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def build_comprehensive(account_address: str, network: str, lang: str = "fa") -> str:
    """Return a fully formatted HTML report string."""
    info = Info(_api_url(network), skip_ws=True)
    addr = account_address

    # ---- 1. Perp account state ----
    try:
        state = info.user_state(addr)
    except Exception as e:
        return (f"❌ خطا در دریافت وضعیت حساب: {e}" if lang == "fa"
                else f"❌ Failed to fetch account state: {e}")
    ms = state.get("marginSummary", {}) or {}
    account_value = _f(ms.get("accountValue"))
    total_ntl_pos = _f(ms.get("totalNtlPos"))
    total_margin_used = _f(ms.get("totalMarginUsed"))
    withdrawable = _f(state.get("withdrawable"))

    # ---- 2. Spot balances ----
    spot_usdc_total = 0.0
    spot_usdc_hold = 0.0
    other_spot = []
    try:
        sp = info.spot_user_state(addr)
        for b in sp.get("balances", []):
            coin = b.get("coin")
            tot = _f(b.get("total"))
            hold = _f(b.get("hold"))
            if coin == "USDC":
                spot_usdc_total = tot
                spot_usdc_hold = hold
            elif tot > 0:
                other_spot.append((coin, tot))
    except Exception:
        pass
    spot_usdc_free = max(spot_usdc_total - spot_usdc_hold, 0.0)

    # ---- 3. Open positions ----
    positions = []
    for p in state.get("assetPositions", []):
        pos = p.get("position", {}) or {}
        szi = _f(pos.get("szi"))
        if szi == 0:
            continue
        lev = pos.get("leverage")
        lev_val = _f(lev.get("value")) if isinstance(lev, dict) else _f(lev)
        positions.append({
            "coin": pos.get("coin"),
            "side": "long" if szi > 0 else "short",
            "size": abs(szi),
            "entry": _f(pos.get("entryPx")),
            "upnl": _f(pos.get("unrealizedPnl")),
            "lev": lev_val,
            "liq": pos.get("liquidationPx"),
            "margin": _f(pos.get("marginUsed")),
            "roe": _f(pos.get("returnOnEquity")) * 100,
        })

    # ---- 4. Open orders ----
    try:
        open_orders = info.open_orders(addr) or []
    except Exception:
        open_orders = []

    # ---- 5. Full fill history: fees, volume, realized pnl ----
    try:
        fills = info.user_fills(addr) or []
    except Exception:
        fills = []
    total_fee = 0.0
    total_volume = 0.0            # notional traded (px*sz) across all fills
    total_realized_pnl = 0.0
    wins = losses = 0
    fills_count = len(fills)
    for f in fills:
        total_fee += _f(f.get("fee"))
        total_volume += _f(f.get("px")) * _f(f.get("sz"))
        pnl = _f(f.get("closedPnl"))
        if pnl > 0:
            wins += 1
            total_realized_pnl += pnl
        elif pnl < 0:
            losses += 1
            total_realized_pnl += pnl
    closed_count = wins + losses
    win_rate = round(wins / closed_count * 100, 1) if closed_count else 0.0

    # ---- 6. Funding paid/received (full available history) ----
    total_funding = 0.0
    funding_count = 0
    try:
        # pull a wide window (90 days) to capture most funding events
        since_ms = int((time.time() - 90 * 86400) * 1000)
        fh = info.user_funding_history(addr, since_ms) or []
        for rec in fh:
            delta = rec.get("delta", {}) or {}
            total_funding += _f(delta.get("usdc"))
            funding_count += 1
    except Exception:
        pass

    # ---- 7. Deposits / withdrawals ledger (non-USD-transfer ledger updates) ----
    deposits = 0.0
    withdrawals = 0.0
    try:
        since_ms = int((time.time() - 365 * 86400) * 1000)
        ledger = info.user_non_funding_ledger_updates(addr, since_ms) or []
        for rec in ledger:
            delta = rec.get("delta", {}) or {}
            dtype = delta.get("type", "")
            usdc = _f(delta.get("usdc"))
            if dtype == "deposit":
                deposits += usdc
            elif dtype == "withdraw":
                withdrawals += abs(usdc)
    except Exception:
        pass

    # ---- 8. 24h market volume for our symbols (from asset ctxs) ----
    market_vol = {}
    try:
        meta, ctxs = info.meta_and_asset_ctxs()
        names = [a["name"] for a in meta.get("universe", [])]
        watch = {p["coin"] for p in positions} or {"BTC", "ETH", "BNB"}
        for sym in watch:
            if sym in names:
                c = ctxs[names.index(sym)]
                market_vol[sym] = {
                    "day_vol": _f(c.get("dayNtlVlm")),
                    "funding": _f(c.get("funding")),
                    "oi": _f(c.get("openInterest")),
                    "mark": _f(c.get("markPx")),
                }
    except Exception:
        pass

    # ================= FORMAT (Persian / English) =================
    total_equity = account_value + spot_usdc_free
    net_deposit = deposits - withdrawals

    if lang == "fa":
        L = []
        L.append(f"📊 <b>گزارش جامع حساب</b> ({network})")
        L.append(f"<i>{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}</i>")
        L.append("━━━━━━━━━━━━━━━")

        L.append("💰 <b>موجودی و وضعیت حساب</b>")
        L.append(f"• ارزش کل (پرپ+اسپات): <b>{total_equity:.2f}$</b>")
        L.append(f"• حساب پرپ (accountValue): {account_value:.2f}$")
        L.append(f"• اسپات USDC آزاد: {spot_usdc_free:.2f}$")
        if spot_usdc_hold > 0:
            L.append(f"• اسپات قفل‌شده: {spot_usdc_hold:.2f}$")
        L.append(f"• قابل برداشت: <b>{withdrawable:.2f}$</b>")
        L.append(f"• مارجین استفاده‌شده: {total_margin_used:.2f}$")
        L.append(f"• ارزش کل پوزیشن‌ها: {total_ntl_pos:.2f}$")
        if other_spot:
            extras = ", ".join(f"{c}:{v:g}" for c, v in other_spot)
            L.append(f"• کوین‌های اسپات دیگر: {extras}")
        L.append("")

        L.append("📈 <b>سود و زیان</b>")
        L.append(f"• P&L محقق‌شده (کل تاریخچه): <b>{total_realized_pnl:+.2f}$</b>")
        upnl_sum = sum(p["upnl"] for p in positions)
        L.append(f"• P&L باز (پوزیشن‌های فعلی): <b>{upnl_sum:+.2f}$</b>")
        L.append(f"• برد/باخت: {wins}✅ / {losses}❌ (نرخ برد {win_rate}%)")
        L.append("")

        L.append("💸 <b>کارمزد و فاندینگ</b>")
        L.append(f"• کل کارمزد پرداختی (fee): <b>{total_fee:.4f}$</b>")
        L.append(f"• کل فاندینگ (دریافت+/پرداخت-): <b>{total_funding:+.4f}$</b> ({funding_count} رکورد)")
        L.append("")

        L.append("🔄 <b>حجم و تراکنش‌ها</b>")
        L.append(f"• حجم کل معاملات ما (notional): <b>{total_volume:,.2f}$</b>")
        L.append(f"• تعداد کل fill‌ها: {fills_count}")
        L.append(f"• کل واریز شده: {deposits:,.2f}$")
        L.append(f"• کل خارج شده (برداشت): {withdrawals:,.2f}$")
        L.append(f"• خالص واریز: <b>{net_deposit:+,.2f}$</b>")
        L.append("")

        if market_vol:
            L.append("🌐 <b>حجم ۲۴ ساعته بازار (ارزهای ما)</b>")
            for sym, m in market_vol.items():
                L.append(f"• {sym}: {m['day_vol']:,.0f}$ | فاندینگ {m['funding']*100:.4f}% | OI {m['oi']:,.0f}")
            L.append("")

        L.append(f"📌 <b>پوزیشن‌های باز ({len(positions)})</b>")
        if positions:
            for p in positions:
                liq = f"{_f(p['liq']):.2f}" if p["liq"] else "—"
                arrow = "🟢" if p["side"] == "long" else "🔴"
                L.append(
                    f"{arrow} {p['coin']} {p['side'].upper()} x{p['lev']:g}\n"
                    f"   حجم {p['size']:g} | ورود {p['entry']:g} | "
                    f"uPnL {p['upnl']:+.2f}$ (ROE {p['roe']:+.2f}%)\n"
                    f"   لیکویید {liq} | مارجین {p['margin']:.2f}$"
                )
        else:
            L.append("• پوزیشن بازی نیست")
        L.append("")

        L.append(f"📋 <b>سفارش‌های باز ({len(open_orders)})</b>")
        if open_orders:
            for o in open_orders[:12]:
                ro = " (کاهشی)" if o.get("reduceOnly") else ""
                sd = "خرید" if o.get("side") == "B" else "فروش"
                L.append(f"• {o.get('coin')} {sd} {o.get('sz')} @ {o.get('limitPx')}{ro}")
            if len(open_orders) > 12:
                L.append(f"• ... و {len(open_orders)-12} سفارش دیگر")
        else:
            L.append("• سفارش بازی نیست")

        return "\n".join(L)

    # ---- English ----
    L = []
    L.append(f"📊 <b>Comprehensive Account Report</b> ({network})")
    L.append(f"<i>{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}</i>")
    L.append("━━━━━━━━━━━━━━━")
    L.append("💰 <b>Balance & account state</b>")
    L.append(f"• Total equity (perp+spot): <b>{total_equity:.2f}$</b>")
    L.append(f"• Perp account value: {account_value:.2f}$")
    L.append(f"• Spot USDC free: {spot_usdc_free:.2f}$")
    if spot_usdc_hold > 0:
        L.append(f"• Spot on hold: {spot_usdc_hold:.2f}$")
    L.append(f"• Withdrawable: <b>{withdrawable:.2f}$</b>")
    L.append(f"• Margin used: {total_margin_used:.2f}$")
    L.append(f"• Total position notional: {total_ntl_pos:.2f}$")
    L.append("")
    L.append("📈 <b>Profit & Loss</b>")
    L.append(f"• Realized P&L (all-time): <b>{total_realized_pnl:+.2f}$</b>")
    upnl_sum = sum(p["upnl"] for p in positions)
    L.append(f"• Unrealized P&L (open): <b>{upnl_sum:+.2f}$</b>")
    L.append(f"• Wins/Losses: {wins}✅ / {losses}❌ (win rate {win_rate}%)")
    L.append("")
    L.append("💸 <b>Fees & funding</b>")
    L.append(f"• Total fees paid: <b>{total_fee:.4f}$</b>")
    L.append(f"• Total funding (recv+/paid-): <b>{total_funding:+.4f}$</b> ({funding_count} recs)")
    L.append("")
    L.append("🔄 <b>Volume & transfers</b>")
    L.append(f"• Our total traded volume: <b>{total_volume:,.2f}$</b>")
    L.append(f"• Total fills: {fills_count}")
    L.append(f"• Total deposited: {deposits:,.2f}$")
    L.append(f"• Total withdrawn: {withdrawals:,.2f}$")
    L.append(f"• Net deposit: <b>{net_deposit:+,.2f}$</b>")
    L.append("")
    if market_vol:
        L.append("🌐 <b>24h market volume (our coins)</b>")
        for sym, m in market_vol.items():
            L.append(f"• {sym}: {m['day_vol']:,.0f}$ | funding {m['funding']*100:.4f}% | OI {m['oi']:,.0f}")
        L.append("")
    L.append(f"📌 <b>Open positions ({len(positions)})</b>")
    if positions:
        for p in positions:
            liq = f"{_f(p['liq']):.2f}" if p["liq"] else "—"
            arrow = "🟢" if p["side"] == "long" else "🔴"
            L.append(
                f"{arrow} {p['coin']} {p['side'].upper()} x{p['lev']:g}\n"
                f"   size {p['size']:g} | entry {p['entry']:g} | "
                f"uPnL {p['upnl']:+.2f}$ (ROE {p['roe']:+.2f}%)\n"
                f"   liq {liq} | margin {p['margin']:.2f}$"
            )
    else:
        L.append("• No open positions")
    L.append("")
    L.append(f"📋 <b>Open orders ({len(open_orders)})</b>")
    if open_orders:
        for o in open_orders[:12]:
            ro = " (reduceOnly)" if o.get("reduceOnly") else ""
            L.append(f"• {o.get('coin')} {o.get('side')} {o.get('sz')} @ {o.get('limitPx')}{ro}")
        if len(open_orders) > 12:
            L.append(f"• ... and {len(open_orders)-12} more")
    else:
        L.append("• No open orders")
    return "\n".join(L)
