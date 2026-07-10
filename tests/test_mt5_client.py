from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import MetaTrader5 as mt5
from swift_scanner.config import ScannerConfig, resolve_symbol_alias
from swift_scanner.mt5_client import MT5Client


def make_client() -> MT5Client:
    return MT5Client(ScannerConfig())


def test_has_open_position_returns_true_when_positions_exist(monkeypatch):
    client = make_client()
    monkeypatch.setattr(mt5, "positions_get", lambda symbol: [Mock()])

    assert client.has_open_position("EURUSD") is True


def test_has_open_position_returns_false_when_no_positions_and_no_error(monkeypatch):
    client = make_client()
    monkeypatch.setattr(mt5, "positions_get", lambda symbol: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (0, "no error"))

    assert client.has_open_position("EURUSD") is False


def test_is_symbol_tradeable_returns_expected_value(monkeypatch):
    client = make_client()

    symbol_info = Mock()
    symbol_info.trade_allowed = True
    symbol_info.name = "EURUSD"
    monkeypatch.setattr(mt5, "symbol_info", lambda symbol: symbol_info)

    assert client.is_symbol_tradeable("EURUSD") is True


def test_get_symbol_market_status_includes_trade_fields(monkeypatch):
    client = make_client()

    symbol_info = Mock()
    symbol_info.name = "EURUSD"
    symbol_info.visible = True
    symbol_info.trade_allowed = False
    symbol_info.trade_tick_size = 0.00001
    symbol_info.point = 0.0001
    symbol_info.spread = 2
    symbol_info.digits = 5
    symbol_info.volume_step = 0.01
    symbol_info.volume_min = 0.01
    symbol_info.volume_max = 100.0
    monkeypatch.setattr(mt5, "symbol_info", lambda symbol: symbol_info)

    status = client.get_symbol_market_status("EURUSD")

    assert status["symbol"] == "EURUSD"
    assert status["visible"] is True
    assert status["trade_allowed"] is True
    assert status["tick_size"] == 0.00001
    assert status["point"] == 0.0001
    assert status["spread"] == 2
    assert status["digits"] == 5
    assert status["volume_step"] == 0.01
    assert status["volume_min"] == 0.01
    assert status["volume_max"] == 100.0


def test_normalize_order_volume_rounds_up_to_lot_constraints(monkeypatch):
    client = make_client()

    symbol_info = Mock()
    symbol_info.name = "US30_x10"
    symbol_info.visible = True
    symbol_info.trade_allowed = True
    symbol_info.trade_tick_size = 0.1
    symbol_info.point = 0.1
    symbol_info.spread = 1
    symbol_info.digits = 2
    symbol_info.volume_step = 0.1
    symbol_info.volume_min = 0.1
    symbol_info.volume_max = 10.0
    monkeypatch.setattr(mt5, "symbol_info", lambda symbol: symbol_info)

    assert client.normalize_order_volume("US30_x10", 0.01) == 0.1
    assert client.normalize_order_volume("US30_x10", 0.05) == 0.1
    assert client.normalize_order_volume("US30_x10", 0.15) == 0.2
    assert client.normalize_order_volume("US30_x10", 5.0) == 5.0


def test_close_position_partial_targets_original_position_ticket(monkeypatch):
    client = make_client()
    sent_requests = []

    monkeypatch.setattr(
        mt5,
        "symbol_info_tick",
        lambda symbol: SimpleNamespace(ask=101.0, bid=100.5),
    )
    monkeypatch.setattr(
        mt5,
        "order_send",
        lambda request: sent_requests.append(request)
        or SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE, order=987),
    )

    ticket = client.close_position_partial(
        "US30m",
        "bullish",
        0.1,
        deviation=15,
        magic=123456,
        position_ticket=555,
    )

    assert ticket == 987
    assert sent_requests[0]["position"] == 555
    assert sent_requests[0]["type"] == mt5.ORDER_TYPE_SELL


def test_close_position_partial_accepts_deal_ticket(monkeypatch):
    client = make_client()

    monkeypatch.setattr(
        mt5,
        "symbol_info_tick",
        lambda symbol: SimpleNamespace(ask=101.0, bid=100.5),
    )
    monkeypatch.setattr(
        mt5,
        "order_send",
        lambda request: SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE, order=0, deal=654),
    )

    assert client.close_position_partial("US30m", "bullish", 0.1, position_ticket=555) == 654


def test_resolve_watchlist_matches_broker_aliases(monkeypatch):
    client = make_client()

    available_symbols = []
    for name in [
        "BTCUSDm",
        "XAUUSDm",
        "XAUUSDM",
        "USTEC_x100m",
        "US30_x10",
        "ETHUSDm",
        "GBPUSDm",
        "USDJPYm",
        "EURUSDm",
        "AUDUSDm",
        "WTIUSD",
    ]:
        symbol = Mock()
        symbol.name = name
        available_symbols.append(symbol)

    monkeypatch.setattr(mt5, "symbols_get", lambda: available_symbols)
    monkeypatch.setattr(mt5, "symbol_select", lambda symbol, visible: True)

    matches = client.resolve_watchlist()
    actual_by_requested = {match.requested: match.actual for match in matches}

    assert actual_by_requested["BTCUSD"] == "BTCUSDm"
    assert actual_by_requested["XAUUSD"] == "XAUUSDm"
    assert actual_by_requested["NASDAQ"] == "USTEC_x100m"
    assert actual_by_requested["US30"] == "US30_x10"
    assert actual_by_requested["ETHUSD"] == "ETHUSDm"
    assert actual_by_requested["GBPUSD"] == "GBPUSDm"
    assert actual_by_requested["USDJPY"] == "USDJPYm"
    assert actual_by_requested["EURUSD"] == "EURUSDm"
    assert actual_by_requested["AUDUSD"] == "AUDUSDm"
    assert actual_by_requested["USOIL"] == "WTIUSD"


def test_resolve_watchlist_prefers_us30_x10m_when_available(monkeypatch):
    client = make_client()

    available_symbols = []
    for name in [
        "BTCUSDm",
        "XAUUSDm",
            "XAUUSDM",
            "USTEC_x100m",
            "US30_x10m",
            "US30_x10",
            "US30m",
        "ETHUSDm",
        "GBPUSDm",
        "USDJPYm",
        "EURUSDm",
        "AUDUSDm",
        "WTIUSD",
    ]:
        symbol = Mock()
        symbol.name = name
        available_symbols.append(symbol)

    monkeypatch.setattr(mt5, "symbols_get", lambda: available_symbols)
    monkeypatch.setattr(mt5, "symbol_select", lambda symbol, visible: True)

    matches = client.resolve_watchlist()
    actual_by_requested = {match.requested: match.actual for match in matches}

    assert actual_by_requested["US30"] == "US30_x10m"


def test_resolve_symbol_alias_matches_all_broker_aliases():
    assert resolve_symbol_alias("XAUUSDM") == "XAUUSD"
    assert resolve_symbol_alias("BTCUSDm") == "BTCUSD"
    assert resolve_symbol_alias("ETHUSDm") == "ETHUSD"
    assert resolve_symbol_alias("USTEC_x100m") == "NASDAQ"
    assert resolve_symbol_alias("US30_x10") == "US30"
    assert resolve_symbol_alias("EURUSDm") == "EURUSD"
    assert resolve_symbol_alias("USDJPYm") == "USDJPY"
    assert resolve_symbol_alias("GBPUSDm") == "GBPUSD"
    assert resolve_symbol_alias("AUDUSDm") == "AUDUSD"
    assert resolve_symbol_alias("WTIUSD") == "USOIL"
