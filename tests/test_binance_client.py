"""Offline tests for the Binance client helpers.

These do not hit the network — they verify the symbol conversion (a real bug
class: ccxt needs unified symbols) and that the exchange object builds with the
expected configuration.
"""
import pytest

from maya.config import RunMode, Settings
from maya.data.binance_client import build_exchange, to_ccxt_symbol


@pytest.mark.parametrize("raw,expected", [
    ("BTCUSDT", "BTC/USDT:USDT"),
    ("ETHUSDT", "ETH/USDT:USDT"),
    ("SOLUSDC", "SOL/USDC:USDC"),
    ("BTC/USDT:USDT", "BTC/USDT:USDT"),  # already unified
    ("eth-usdt", "ETH/USDT:USDT"),
])
def test_to_ccxt_symbol(raw, expected):
    assert to_ccxt_symbol(raw) == expected


def test_to_ccxt_symbol_rejects_garbage():
    with pytest.raises(ValueError):
        to_ccxt_symbol("NOTAPAIR")


def test_build_exchange_offline():
    pytest.importorskip("ccxt")
    ex = build_exchange(Settings(mode=RunMode.PAPER, symbol="BTCUSDT"))
    assert ex.options.get("defaultType") == "future"
    assert ex.has.get("fetchOHLCV")


def test_build_exchange_testnet_sandbox():
    pytest.importorskip("ccxt")
    ex = build_exchange(Settings(mode=RunMode.TESTNET, symbol="BTCUSDT"))
    # Sandbox mode swaps in the testnet URLs.
    url = ex.urls.get("api")
    api_str = str(url)
    assert "testnet" in api_str or "binancefuture" in api_str
