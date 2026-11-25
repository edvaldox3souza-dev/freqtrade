"""Testes TDD para garantir que micro-bancas ($16 USDT) nao sejam travadas por limites artificiais."""

import pathlib
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pandas as pd

for mod in ["talib", "talib.abstract", "freqtrade", "freqtrade.persistence", "freqtrade.strategy"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()


class MockIStrategy:
    pass


sys.modules["freqtrade.strategy"].IStrategy = MockIStrategy

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from user_data.strategies.RenderQuantV1 import RenderQuantV1  # noqa: E402
from user_data.strategies.RenderQuantV3_Defensive import RenderQuantV3_Defensive  # noqa: E402
from user_data.strategies.RenderQuantV4_Adaptive import RenderQuantV4_Adaptive  # noqa: E402


def _make_mock_trade(pair: str, open_rate: float, amount: float) -> MagicMock:
    t = MagicMock()
    t.pair = pair
    t.is_open = True
    t.open_rate = open_rate
    t.current_rate = open_rate
    t.stop_loss_or_limit = open_rate * 0.96
    t.amount = amount
    t.calc_profit_abs.return_value = 0.0
    t.realized_profit = 0.0
    return t


def test_micro_wallet_16_usdt_permite_todos_pares_kraken_e_alta_volatilidade():
    """Com $16 USDT, lotes reais da Kraken ($2.93-$4.85) devem passar mesmo com ATR elevado."""
    now = datetime.now(UTC)
    df_high_atr = pd.DataFrame([{"atr_pct": 0.030, "market_regime_bull": 0}])

    for cls in (RenderQuantV1, RenderQuantV3_Defensive, RenderQuantV4_Adaptive):
        strat = cls()
        strat.wallets = MagicMock()
        strat.wallets.get_total_stake_amount.return_value = 16.0
        strat.dp = MagicMock()
        strat.dp.get_analyzed_dataframe.return_value = (df_high_atr, now)

        for min_stake in (2.93, 3.37, 3.76, 4.10, 4.85):
            stake = strat.custom_stake_amount(
                pair="XRP/USDT",
                current_time=now,
                current_rate=0.94,
                proposed_stake=1.60,
                min_stake=min_stake,
                max_stake=15.84,
                leverage=1.0,
                entry_tag=None,
                side="long",
            )
            assert stake == min_stake, f"{cls.__name__} bloqueou min_stake={min_stake} em banca de $16"


def test_micro_wallet_16_usdt_permite_3_trades_e_2_no_mesmo_cluster():
    """Em micro-banca de $16, Portfolio Heat (12%) e Cluster (2) nao devem travar 2a/3a operacao."""
    now = datetime.now(UTC)
    strat = RenderQuantV1()
    strat.wallets = MagicMock()
    strat.wallets.get_total_stake_amount.return_value = 16.0
    strat.wallets.get_available_stake_amount.return_value = 8.50
    strat.config = {"stake_currency": "USDT", "runmode": "backtest"}
    strat.dp = MagicMock()
    strat.dp.runmode = "backtest"
    strat.dp.ticker.return_value = {"bid": 0.1465, "ask": 0.1469}  # spread ~0.273% (<= 0.30%)
    strat.dp.get_analyzed_dataframe.return_value = (
        pd.DataFrame([{"close": 60000.0, "sma50": 59500.0, "ema9": 60100.0, "ema21": 59900.0, "macd_hist": 10.0}]),
        now,
    )

    open_trades = [
        _make_mock_trade("SOL/USDT", 70.0, 0.06),  # $4.20 no cluster LAYER1
        _make_mock_trade("ETH/USDT", 1680.0, 0.0025),  # $4.20 no cluster MAJORS
    ]

    with (
        patch("user_data.strategies.RenderQuantV1.Trade.get_trades_proxy", side_effect=[[], open_trades, open_trades, open_trades]),
    ):
        ok = strat.confirm_trade_entry(
            pair="ADA/USDT",  # tambem LAYER1 (2o trade do cluster!) e 3o trade aberto (~3.15% heat)
            order_type="limit",
            amount=30.0,
            rate=0.1465,
            time_in_force="gtc",
            current_time=now,
            entry_tag="V1:oversold_reclaim",
            side="long",
        )
    assert ok is True


def test_btc_macro_guard_nao_trava_por_oscilacao_leve_abaixo_sma50():
    """BTC 1.1% abaixo da SMA50 sem momentum de baixa dupla ou com retomada a -2% deve liberar altcoin."""
    now = datetime.now(UTC)
    strat = RenderQuantV1()
    strat.wallets = MagicMock()
    strat.wallets.get_total_stake_amount.return_value = 16.0
    strat.wallets.get_available_stake_amount.return_value = 16.0
    strat.config = {"stake_currency": "USDT", "runmode": "backtest"}
    strat.dp = MagicMock()
    strat.dp.runmode = "backtest"
    strat.dp.ticker.return_value = {"bid": 0.1375, "ask": 0.1378}

    # Caso 1: BTC -1.1% abaixo da SMA50, mas MACD positivo (retomando) -> deve LIBERAR (caso real 14:15Z)
    strat.dp.get_analyzed_dataframe.return_value = (
        pd.DataFrame([{"close": 58334.0, "sma50": 59000.0, "ema9": 58400.0, "ema21": 58300.0, "macd_hist": 15.0}]),
        now,
    )
    assert (
        strat.confirm_trade_entry(
            "ADA/USDT", "limit", 21.0, 0.1375, "gtc", now, "V1:oversold_reclaim", "long"
        )
        is True
    )

    # Caso 2: BTC -3.5% abaixo da SMA50 (crash real) -> continua VETANDO por seguranca
    strat.dp.get_analyzed_dataframe.return_value = (
        pd.DataFrame([{"close": 56900.0, "sma50": 59000.0, "ema9": 57000.0, "ema21": 57500.0, "macd_hist": -80.0}]),
        now,
    )
    assert (
        strat.confirm_trade_entry(
            "ADA/USDT", "limit", 21.0, 0.1375, "gtc", now, "V1:oversold_reclaim", "long"
        )
        is False
    )
