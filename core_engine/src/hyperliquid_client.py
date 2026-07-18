"""
Thin wrapper around the official hyperliquid-python-sdk. Defaults to
testnet unless config explicitly says "mainnet". Places the entry order
plus real stop-loss/take-profit trigger orders on the exchange itself
(not just monitored in-process). If placing SL/TP fails right after
entry, the position is closed immediately rather than left unprotected.
"""
import logging
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account

logger = logging.getLogger("hyperliquid_client")


class HyperliquidClient:
    def __init__(self, account_address: str, secret_key: str, network: str = "testnet"):
        api_url = constants.TESTNET_API_URL if network == "testnet" else constants.MAINNET_API_URL
        self.account_address = account_address
        self.info = Info(api_url, skip_ws=True)
        # Exchange needs a signing wallet (LocalAccount), not a raw hex string.
        wallet = Account.from_key(secret_key)
        self.exchange = Exchange(wallet, api_url, account_address=account_address)
        self.network = network
        logger.info(f"Hyperliquid client initialized on {network}")

    def get_candles(self, symbol: str, interval: str, lookback_count: int = 200):
        """interval like '15m','1h','4h'. Returns raw candle list from Info API."""
        import time
        end = int(time.time() * 1000)
        unit_ms = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
        n = int("".join(filter(str.isdigit, interval)))
        u = "".join(filter(str.isalpha, interval))
        candle_ms = n * unit_ms.get(u, 60_000)
        start = end - candle_ms * lookback_count
        return self.info.candles_snapshot(symbol, interval, start, end)

    def get_account_state(self):
        return self.info.user_state(self.account_address)

    def get_usdc_balance(self) -> float:
        """Total USDC the wallet holds across BOTH the spot wallet and the
        perp (futures) margin wallet — counted exactly once.

        On Hyperliquid, opening a perp position puts the margin on HOLD in
        the spot wallet (spot balance `hold` field grows) and the same
        amount shows up as perp `accountValue`. If we blindly summed spot
        `total` + perp `accountValue` we'd double-count that margin.

        Correct total = (spot total - spot hold)  +  perp accountValue
                       = spot free USDC             +  perp margin
        This works whether the user keeps everything in spot, everything in
        perp, or a mix — each dollar is counted exactly once.
        """
        total = 0.0
        # 1) spot free USDC = total minus the portion held as perp margin
        try:
            spot = self.info.spot_user_state(self.account_address)
            for b in spot.get("balances", []):
                if b.get("coin") == "USDC":
                    total += float(b.get("total", 0)) - float(b.get("hold", 0))
        except Exception:
            pass
        # 2) perp margin accountValue (already reflects the held margin)
        try:
            state = self.get_account_state()
            total += float(state.get("marginSummary", {}).get("accountValue", 0))
        except Exception:
            pass
        return max(total, 0.0)

    def get_open_positions(self) -> list[dict]:
        """Returns all open positions with side, size, entryPx, unrealizedPnl, leverage."""
        state = self.get_account_state()
        out = []
        for p in state.get("assetPositions", []):
            pos = p.get("position", {})
            size = float(pos.get("szi", 0))
            if size == 0:
                continue
            out.append({
                "symbol": pos.get("coin"),
                "side": "long" if size > 0 else "short",
                "size": abs(size),
                "entry_price": float(pos.get("entryPx", 0)),
                "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                "leverage": float(pos.get("leverage", {}).get("value", 0)) if isinstance(pos.get("leverage"), dict) else None,
            })
        return out

    def get_market_context(self, symbol: str) -> dict:
        """Best-effort 'fundamental' snapshot for one symbol: funding rate,
        open interest, 24h notional volume and 24h price change. This is
        what the AI pipeline's fundamental-analysis stage reads instead of
        (or alongside) real news, since a lightweight self-hosted bot has
        no reliable live news feed -- funding/OI/volume are the actual
        on-exchange "fundamentals" for a perp market and are always
        available with zero extra API keys.
        Returns {} on any failure so a market-data hiccup never blocks a
        trading cycle -- the fundamental stage just reasons with less.
        """
        try:
            meta, asset_ctxs = self.info.meta_and_asset_ctxs()
            names = [a["name"] for a in meta.get("universe", [])]
            idx = names.index(symbol)
            ctx = asset_ctxs[idx]
            mark_px = float(ctx.get("markPx", 0) or 0)
            prev_day_px = float(ctx.get("prevDayPx", 0) or 0)
            day_change_pct = (
                round((mark_px - prev_day_px) / prev_day_px * 100, 2)
                if prev_day_px else None
            )
            return {
                "mark_price": mark_px,
                "funding_rate": float(ctx.get("funding", 0) or 0),
                "open_interest": float(ctx.get("openInterest", 0) or 0),
                "day_notional_volume_usd": float(ctx.get("dayNtlVlm", 0) or 0),
                "day_change_pct": day_change_pct,
                "premium": float(ctx.get("premium", 0) or 0) if ctx.get("premium") is not None else None,
            }
        except Exception as e:
            logger.warning(f"get_market_context failed for {symbol}: {e}")
            return {}

    def get_current_position(self, symbol: str) -> dict | None:
        for p in self.get_open_positions():
            if p["symbol"] == symbol:
                return p
        return None

    def set_leverage(self, symbol: str, leverage: int):
        self.exchange.update_leverage(leverage, symbol, is_cross=True)

    def transfer_between_wallets(self, amount: float, to_perp: bool) -> dict:
        """Move USDC between SPOT and PERP wallets.
        to_perp=True  -> spot -> perp (futures)
        to_perp=False -> perp -> spot
        Returns a dict with ok/msg + balances before/after.
        """
        try:
            spot_before = self.get_spot_usdc()
            perp_before = self.get_perp_usdc()
            amt = round(float(amount), 2)
            if amt <= 0:
                return {"ok": False, "msg": "مقدار باید بزرگتر از صفر باشد", "spot_before": spot_before, "perp_before": perp_before}
            # The SDK returns a dict (not raising) on failure, so inspect it.
            resp = self.exchange.usd_class_transfer(amt, to_perp)
            if isinstance(resp, dict) and resp.get("status") == "err":
                detail = resp.get("response", "unknown error")
                # Friendly message for the common unified-account case
                if "unified" in str(detail).lower():
                    return {"ok": False,
                            "msg": "انتقال انجام نشد: حساب شما در حالت UNIFIED است و انتقال اسپات↔فیوچر غیرفعال است. لطفاً حساب را به حالت عادی (CLASSIC) تغییر دهید یا مستقیماً از والت متحد استفاده کنید.",
                            "spot_before": spot_before, "perp_before": perp_before}
                return {"ok": False, "msg": f"انتقال انجام نشد: {detail}",
                        "spot_before": spot_before, "perp_before": perp_before}
            # give the chain a moment to settle
            import time
            time.sleep(2)
            spot_after = self.get_spot_usdc()
            perp_after = self.get_perp_usdc()
            direction = "SPOT→PERP" if to_perp else "PERP→SPOT"
            return {
                "ok": True,
                "msg": f"{amt:.2f} USDC منتقل شد ({direction})",
                "direction": direction,
                "amount": amt,
                "spot_before": spot_before, "perp_before": perp_before,
                "spot_after": spot_after, "perp_after": perp_after,
            }
        except Exception as e:
            logger.warning(f"transfer between wallets failed: {e}")
            return {"ok": False, "msg": f"خطا: {e}", "spot_before": 0, "perp_before": 0}

    def get_spot_usdc(self) -> float:
        try:
            spot = self.info.spot_user_state(self.account_address)
            for b in spot.get("balances", []):
                if b.get("coin") == "USDC":
                    return float(b.get("total", 0))
        except Exception:
            pass
        return 0.0

    def get_perp_usdc(self) -> float:
        try:
            state = self.get_account_state()
            return float(state.get("marginSummary", {}).get("accountValue", 0))
        except Exception:
            return 0.0

    def ensure_perp_funds(self, keep_in_spot: float = 1.0) -> float:
        """Perp (futures) trading draws margin from the PERP wallet, but
        deposits/faucet USDC land in the SPOT wallet. If perp is empty but
        spot has funds, move spot->perp (leaving a tiny buffer in spot).
        Returns the amount transferred."""
        perp = self.get_perp_usdc()
        if perp > 0:
            return 0.0
        spot = self.get_spot_usdc()
        movable = spot - keep_in_spot
        if movable <= 0:
            return 0.0
        try:
            self.exchange.usd_class_transfer(round(movable, 2), True)
            logger.info(f"Auto-transferred {movable:.2f} USDC spot->perp")
            return movable
        except Exception as e:
            logger.warning(f"spot->perp transfer failed: {e}")
            return 0.0

    def _sz_decimals(self, symbol: str) -> int:
        """How many decimals the exchange allows for the order SIZE of this coin."""
        try:
            meta = self.info.meta()
            for a in meta["universe"]:
                if a["name"] == symbol:
                    return int(a["szDecimals"])
        except Exception:
            pass
        return 4  # safe default

    def _round_size(self, symbol: str, size: float) -> float:
        return round(size, self._sz_decimals(symbol))

    def _round_price(self, symbol: str, price: float) -> float:
        """HL perp price rule: max 5 significant figures AND max (6 - szDecimals)
        decimal places. We apply both and keep the tighter one."""
        max_dec = max(0, 6 - self._sz_decimals(symbol))
        # 5 significant figures
        if price > 0:
            import math
            digits_before = max(1, int(math.floor(math.log10(abs(price)))) + 1)
            sig_dec = max(0, 5 - digits_before)
            dec = min(max_dec, sig_dec)
        else:
            dec = max_dec
        return round(price, dec)

    def place_market_order_with_sl_tp(
        self, *, symbol: str, is_buy: bool, size: float,
        stop_loss_price: float, take_profit_price: float,
    ):
        """
        Places a market entry, then stop-loss + take-profit trigger orders
        (reduce-only) on the exchange side. If either trigger order fails
        to place, the position is closed immediately so it's never left
        running without protection.
        """
        size = self._round_size(symbol, size)
        stop_loss_price = self._round_price(symbol, stop_loss_price)
        take_profit_price = self._round_price(symbol, take_profit_price)
        logger.info(f"Rounded order: size={size} sl={stop_loss_price} tp={take_profit_price}")

        entry = self.exchange.market_open(symbol, is_buy, size, None, 0.01)
        logger.info(f"Entry order result: {entry}")

        try:
            sl_order = self.exchange.order(
                symbol, not is_buy, size, stop_loss_price,
                {"trigger": {"triggerPx": stop_loss_price, "isMarket": True, "tpsl": "sl"}},
                reduce_only=True,
            )
            tp_order = self.exchange.order(
                symbol, not is_buy, size, take_profit_price,
                {"trigger": {"triggerPx": take_profit_price, "isMarket": True, "tpsl": "tp"}},
                reduce_only=True,
            )
        except Exception:
            logger.error(f"SL/TP placement failed for {symbol}, closing position immediately")
            try:
                self.exchange.market_close(symbol)
            finally:
                raise

        return {"entry": entry, "stop_loss": sl_order, "take_profit": tp_order}

    def close_position(self, symbol: str):
        return self.exchange.market_close(symbol)

    def close_all_positions(self) -> list[str]:
        closed = []
        for p in self.get_open_positions():
            self.exchange.market_close(p["symbol"])
            closed.append(p["symbol"])
        return closed

    def get_position_pnl(self, symbol: str, side: str, entry_price: float, size: float = 1.0) -> dict:
        """Best-effort PnL calc for a position that just closed on-exchange.
        We read the current mark price as the exit, since the exchange
        already closed it (SL/TP). `size` must be the base-asset position
        size (same units stored in trades.size) so pnl_usd is an actual
        dollar amount, not a raw price delta."""
        try:
            raw = self.get_candles(symbol, "1m", lookback_count=2)
            exit_price = float(raw[-1]["c"]) if raw else entry_price
        except Exception:
            exit_price = entry_price
        price_delta = (exit_price - entry_price) if side == "long" else (entry_price - exit_price)
        pnl_usd = price_delta * abs(size or 0)
        return {
            "exit_price": exit_price,
            "pnl_usd": pnl_usd,
            "held": "n/a",
        }
