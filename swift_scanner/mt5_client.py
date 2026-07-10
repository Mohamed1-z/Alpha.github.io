from __future__ import annotations

from dataclasses import dataclass
import time

import MetaTrader5 as mt5
import pandas as pd

from swift_scanner.config import SYMBOL_ALIASES, ScannerConfig, TimeframeConfig, resolve_symbol_alias

class MT5OrderError(RuntimeError):
    pass


class AutoTradingDisabledError(MT5OrderError):
    pass

def normalize_symbol(value: str) -> str:
    return "".join(char for char in value.upper() if char.isalnum())


@dataclass(slots=True)
class SymbolMatch:
    requested: str
    actual: str | None


class MT5Client:
    def __init__(self, config: ScannerConfig) -> None:
        self.config = config

    def __enter__(self) -> "MT5Client":
        if not mt5.initialize():
            code, message = mt5.last_error()
            raise RuntimeError(
                f"MetaTrader 5 initialization failed with error {code}: {message}. "
                "Make sure the MT5 terminal is open and logged in."
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        mt5.shutdown()

    def list_symbols(self):
        symbols = mt5.symbols_get()
        if symbols is None:
            return []
        return list(symbols)

    def resolve_watchlist(self) -> list[SymbolMatch]:
        available = self.list_symbols()
        if not available:
            return [SymbolMatch(requested=symbol, actual=None) for symbol in self.config.watchlist]

        results: list[SymbolMatch] = []
        for requested in self.config.watchlist:
            resolved = self.resolve_symbol(requested, available)
            if resolved:
                mt5.symbol_select(resolved, True)
            results.append(SymbolMatch(requested=requested, actual=resolved))
        return results

    def resolve_symbol(self, requested: str, available) -> str | None:
        canonical_requested = resolve_symbol_alias(requested) or requested
        aliases = [canonical_requested, *SYMBOL_ALIASES.get(canonical_requested, []), requested]
        normalized_aliases = [
            (normalize_symbol(alias), index)
            for index, alias in enumerate(aliases)
        ]

        best_name = None
        best_score = 999
        best_rank = 9999
        for symbol in available:
            name = symbol.name
            normalized_name = normalize_symbol(name)
            for alias, alias_rank in normalized_aliases:
                if not alias:
                    continue
                score = self.match_score(alias, normalized_name)
                if score is None:
                    continue
                tie_breaker = (score, alias_rank, len(name))
                if tie_breaker < (best_score, best_rank, len(best_name) if best_name else 9999):
                    best_score = score
                    best_rank = alias_rank
                    best_name = name

        return best_name

    @staticmethod
    def match_score(alias: str, normalized_name: str) -> int | None:
        if normalized_name == alias:
            return 0
        if normalized_name.startswith(alias) or alias.startswith(normalized_name):
            return 1
        if alias in normalized_name or normalized_name in alias:
            return 2
        return None

    def fetch_rates(self, symbol: str, timeframe: TimeframeConfig) -> pd.DataFrame:
        rates = mt5.copy_rates_from_pos(symbol, timeframe.mt5_code, 0, self.config.bars_to_fetch)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"No market data returned for {symbol} on {timeframe.name}.")

        frame = pd.DataFrame(rates)
        frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        frame["volume"] = frame["tick_volume"].astype(float)
        frame = frame[["time", "open", "high", "low", "close", "volume"]].copy()

        # Exclude the open candle so the scanner only acts on completed candles.
        if len(frame) > 2:
            frame = frame.iloc[:-1].reset_index(drop=True)
        return frame

    def has_open_position(self, symbol: str) -> bool:
        positions = mt5.positions_get(symbol=symbol)
        if positions is None:
            last_error = mt5.last_error()
            if last_error[0] != 0:
                raise RuntimeError(
                    f"Failed to query open positions for {symbol}. mt5.last_error()={last_error}"
                )
            return False
        return len(positions) > 0

    def get_open_positions(self, symbol: str):
        positions = mt5.positions_get(symbol=symbol)
        if positions is None:
            last_error = mt5.last_error()
            if last_error[0] != 0:
                raise RuntimeError(
                    f"Failed to query open positions for {symbol}. mt5.last_error()={last_error}"
                )
            return []
        return list(positions)

    def get_symbol_market_status(self, symbol: str) -> dict[str, object]:
        info = mt5.symbol_info(symbol)
        if info is None:
            last_error = mt5.last_error()
            # Try to recover by checking the symbol tick if symbol_info is unavailable.
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                raise RuntimeError(
                    f"Could not retrieve symbol info for {symbol}. mt5.last_error()={last_error}"
                )
            return {
                "symbol": symbol,
                "visible": False,
                "trade_allowed": True,
                "tick_size": float(getattr(tick, 'tick_size', 0.0) or 0.0),
                "point": float(getattr(tick, 'point', 0.0) or 0.0),
                "spread": int(getattr(tick, 'spread', 0) or 0),
                "digits": int(getattr(tick, 'digits', 0) or 0),
                "volume_step": 0.0,
                "volume_min": 0.0,
                "volume_max": 0.0,
            }

        trade_allowed = False
        if hasattr(info, 'trade_allowed'):
            trade_allowed = bool(getattr(info, 'trade_allowed'))
        elif hasattr(info, 'trade_mode'):
            trade_allowed = getattr(info, 'trade_mode') == mt5.SYMBOL_TRADE_MODE_FULL

        if getattr(self.config, "force_trade_allowed", False):
            trade_allowed = True

        return {
            "symbol": info.name,
            "visible": bool(getattr(info, 'visible', False)),
            "trade_allowed": trade_allowed,
            "tick_size": float(getattr(info, 'trade_tick_size', 0.0) or 0.0),
            "point": float(getattr(info, 'point', 0.0) or 0.0),
            "spread": int(getattr(info, 'spread', 0) or 0),
            "digits": int(getattr(info, 'digits', 0) or 0),
            "volume_step": float(getattr(info, 'volume_step', 0.0) or 0.0),
            "volume_min": float(getattr(info, 'volume_min', 0.0) or 0.0),
            "volume_max": float(getattr(info, 'volume_max', 0.0) or 0.0),
        }

    def normalize_order_volume(self, symbol: str, volume: float) -> float:
        status = self.get_symbol_market_status(symbol)
        volume = float(volume)
        step = float(status.get("volume_step", 0.0) or 0.0)
        minimum = float(status.get("volume_min", 0.0) or 0.0)
        maximum = float(status.get("volume_max", 0.0) or 0.0)

        if maximum > 0 and volume > maximum:
            volume = maximum

        if step > 0.0:
            import math

            volume = math.ceil(volume / step) * step

        if minimum > 0 and volume < minimum:
            volume = minimum

        if maximum > 0 and volume > maximum:
            volume = maximum

        return float(round(volume, 8))

    def is_symbol_tradeable(self, symbol: str) -> bool:
        # If the config forces trade_allowed, always return True
        if getattr(self.config, "force_trade_allowed", False):
            return True

        info = mt5.symbol_info(symbol)
        if info is None:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                last_error = mt5.last_error()
                raise RuntimeError(
                    f"Could not retrieve symbol info for {symbol}. mt5.last_error()={last_error}"
                )
            return True

        if hasattr(info, 'trade_allowed'):
            return bool(getattr(info, 'trade_allowed'))
        if hasattr(info, 'trade_mode'):
            return getattr(info, 'trade_mode') == mt5.SYMBOL_TRADE_MODE_FULL
        return True

    def get_tick(self, symbol: str):
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            last = mt5.last_error()
            raise RuntimeError(
                f"Could not retrieve tick data for {symbol}. mt5.last_error()={last}"
            )
        return tick

    def place_market_order(
        self,
        symbol: str,
        direction: str,
        volume: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        deviation: int = 20,
        magic: int | None = None,
    ) -> int:
        tick = self.get_tick(symbol)
        if direction == "bullish":
            order_type = mt5.ORDER_TYPE_BUY
            price = float(tick.ask)
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price = float(tick.bid)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "price": price,
            "deviation": int(deviation),
            "magic": int(magic) if magic is not None else 0,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        if stop_loss is not None:
            request["sl"] = float(stop_loss)
        if take_profit is not None:
            request["tp"] = float(take_profit)

        result = mt5.order_send(request)
        if result is None:
            last = mt5.last_error()
            raise RuntimeError(
                f"MetaTrader 5 order_send returned no response. mt5.last_error()={last}"
            )
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            last = mt5.last_error()
            message = (
                f"MT5 order failed: retcode={result.retcode}, comment={getattr(result, 'comment', None)}, "
                f"mt5.last_error()={last}"
            )
            if result.retcode == 10027:
                raise AutoTradingDisabledError(message)
            raise MT5OrderError(message)
        return int(result.order)

    def close_position_partial(
        self,
        symbol: str,
        direction: str,
        volume: float,
        deviation: int = 20,
        magic: int | None = None,
        position_ticket: int | None = None,
    ) -> int | None:
        """Close a partial position at market price. Returns ticket number on success, None on failure."""
        try:
            tick = self.get_tick(symbol)
            if direction == "bullish":
                close_type = mt5.ORDER_TYPE_SELL
                price = float(tick.bid)
            else:
                close_type = mt5.ORDER_TYPE_BUY
                price = float(tick.ask)

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": float(volume),
                "type": close_type,
                "price": price,
                "deviation": int(deviation),
                "magic": int(magic) if magic is not None else 0,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            if position_ticket is not None:
                request["position"] = int(position_ticket)

            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                return None
            ticket = int(getattr(result, "order", 0) or 0)
            if ticket:
                return ticket
            deal = int(getattr(result, "deal", 0) or 0)
            return deal if deal else None
        except Exception:
            return None

    def get_recent_candles(self, symbol: str, timeframe_name: str, count: int = 5) -> list[dict]:
        """Return the last `count` closed candles for a symbol as a list of dicts with open/high/low/close/volume."""
        try:
            tf_map = {
                "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
                "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
                "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
                "D1": mt5.TIMEFRAME_D1,
            }
            tf_code = tf_map.get(timeframe_name.upper(), mt5.TIMEFRAME_M15)
            rates = mt5.copy_rates_from_pos(symbol, tf_code, 1, count)  # skip open candle
            if rates is None or len(rates) == 0:
                return []
            return [
                {"open": r["open"], "high": r["high"], "low": r["low"],
                 "close": r["close"], "volume": r["tick_volume"]}
                for r in rates
            ]
        except Exception:
            return []

    def modify_sl(self, ticket: int, new_sl: float, new_tp: float | None = None) -> bool:
        """Move stop-loss (and optionally take-profit) on an existing position. Returns True on success."""
        # Retry a few times for transient MT5 failures and log details on failure.
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                positions = mt5.positions_get(ticket=ticket)
                if not positions:
                    last = mt5.last_error()
                    print(f"modify_sl: position {ticket} not found. mt5.last_error()={last} (attempt {attempt})")
                    return False
                pos = positions[0]
                request = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "symbol": pos.symbol,
                    "position": ticket,
                    "sl": float(new_sl),
                    "tp": float(new_tp) if new_tp is not None else float(pos.tp),
                }

                result = mt5.order_send(request)
                if result is None:
                    last = mt5.last_error()
                    print(f"modify_sl: order_send returned None for ticket {ticket}. mt5.last_error()={last} (attempt {attempt})")
                elif getattr(result, "retcode", None) == mt5.TRADE_RETCODE_DONE:
                    return True
                else:
                    last = mt5.last_error()
                    comment = getattr(result, "comment", None)
                    print(
                        f"modify_sl: failed to modify SL for {ticket}: retcode={getattr(result,'retcode',None)}, comment={comment}, mt5.last_error()={last} (attempt {attempt})"
                    )
            except Exception as exc:  # pragma: no cover - defensive logging
                last = mt5.last_error()
                print(f"modify_sl: exception while modifying SL for {ticket}: {exc!r}, mt5.last_error()={last} (attempt {attempt})")

            # small delay before retrying transient errors
            if attempt < max_attempts:
                time.sleep(0.5)

        return False

    def get_account_equity(self) -> float | None:
        """Return current account equity, or None if unavailable."""
        try:
            info = mt5.account_info()
            if info is None:
                return None
            return float(info.equity)
        except Exception:
            return None

    def close_position_full(self, ticket: int) -> bool:
        """Fully close an open position at market price (used for weekend flatten). Returns True on success."""
        try:
            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                return False
            pos = positions[0]
            tick = mt5.symbol_info_tick(pos.symbol)
            if tick is None:
                return False
            if pos.type == mt5.ORDER_TYPE_BUY:
                close_type = mt5.ORDER_TYPE_SELL
                price = tick.bid
            else:
                close_type = mt5.ORDER_TYPE_BUY
                price = tick.ask
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": float(pos.volume),
                "type": close_type,
                "position": ticket,
                "price": float(price),
                "deviation": 20,
                "magic": int(pos.magic),
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(request)
            return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        except Exception:
            return False

    def close_position_at_price(
        self,
        symbol: str,
        direction: str,
        volume: float,
        limit_price: float,
        deviation: int = 20,
        magic: int | None = None,
    ) -> int | None:
        """Close position at a specific price level (limit order). Returns ticket number on success."""
        try:
            if direction == "bullish":
                close_type = mt5.ORDER_TYPE_SELL_LIMIT
            else:
                close_type = mt5.ORDER_TYPE_BUY_LIMIT

            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": symbol,
                "volume": float(volume),
                "type": close_type,
                "price": float(limit_price),
                "deviation": int(deviation),
                "magic": int(magic) if magic is not None else 0,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                return None
            return int(result.order)
        except Exception:
            return None
