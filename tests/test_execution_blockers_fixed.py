"""Testes TDD reproduzindo os bugs reais identificados pelos 3 auditores em V1, V4 e state_store."""

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

from user_data import state_store  # noqa: E402
from user_data.strategies.RenderQuantV1 import RenderQuantV1  # noqa: E402
from user_data.strategies.RenderQuantV4_Adaptive import RenderQuantV4_Adaptive  # noqa: E402


def test_v1_nao_colide_enter_long_com_exit_long_no_cenario_21h_btc():
    """Caso real 2026-11-25 21:00:11Z BTC/USDT: RSI=66.7, BB=83.4% gera enter_long=1 e NAO exit_long=1."""
    strat = RenderQuantV1()
    strat.dp = MagicMock()
    strat.dp.runmode = "backtest"

    df = pd.DataFrame({
        "open": [59700.0, 59800.0, 59928.4],
        "high": [59950.0, 60050.0, 60380.0],
        "low": [59650.0, 59750.0, 59900.0],
        "close": [59900.0, 59980.0, 60327.5],
        "volume": [100.0, 120.0, 150.0],
        "sma50": [58800.0, 58900.0, 58990.3],
        "sma150": [59200.0, 59210.0, 59220.0],
        "sma200": [59850.0, 59850.0, 59850.5],
        "ema9": [59700.0, 59820.0, 59959.4],
        "ema21": [59450.0, 59510.0, 59587.1],
        "rsi": [61.0, 63.0, 66.7],
        "natr": [0.60, 0.62, 0.64],
        "bb_lower": [58900.0, 59000.0, 59100.0],
        "bb_middle": [59600.0, 59700.0, 59835.0],
        "bb_upper": [60300.0, 60400.0, 60570.0],
        "bb_pos": [0.70, 0.72, 0.834],
        "macd": [90.0, 100.0, 115.0],
        "macd_signal": [60.0, 61.5, 70.2],
        "macd_hist": [30.0, 38.45, 44.80],
        "pivot_r1": [60600.0, 60600.0, 60600.0],
        "ker": [0.45, 0.48, 0.50],
        "is_whale_vol": [0, 0, 0],
        "vol_modified_z": [0.4, 0.5, 0.8],
        "market_regime_bull": [0, 0, 0],
        "volume_usd_24h": [10_000_000.0] * 3,
        "stretch_atr": [1.4, 1.6, 1.93],
    })

    df = strat.populate_entry_trend(df, {"pair": "BTC/USDT"})
    df = strat.populate_exit_trend(df, {"pair": "BTC/USDT"})

    assert df["enter_long"].iloc[-1] == 1
    assert df.get("exit_long", pd.Series([0] * 3)).iloc[-1] != 1


def test_v1_custom_stoploss_sempre_negativo_e_trava_lucro_corretamente():
    """Freqtrade exige retorno NEGATIVO relativo ao current_rate; positivo causa stop prematuro a -0.6%."""
    strat = RenderQuantV1()
    strat.dp = None
    mock_trade = MagicMock()
    now = datetime.now(UTC)

    sl_be = strat.custom_stoploss("BTC/USDT", mock_trade, now, 61800.0, 0.030, False)
    sl_tier2 = strat.custom_stoploss("BTC/USDT", mock_trade, now, 63600.0, 0.060, False)
    sl_trail = strat.custom_stoploss("BTC/USDT", mock_trade, now, 67200.0, 0.120, False)

    assert sl_be < 0, f"Breakeven stop deve ser negativo no Freqtrade, obteve {sl_be}"
    assert sl_tier2 < 0, f"Tier2 stop deve ser negativo no Freqtrade, obteve {sl_tier2}"
    assert sl_trail < 0, f"Trailing stop deve ser negativo no Freqtrade, obteve {sl_trail}"
    # Com +12% de lucro e atr_pct=1.5% (trailing_dist=2.25%), stop deve ficar em ~-0.0225 (nunca -0.10!)
    assert sl_trail >= -0.025


def test_v1_usa_exit_profit_offset_nativo_freqtrade():
    strat = RenderQuantV1()
    assert getattr(strat, "exit_profit_offset", 0.0) >= 0.010


def test_v1_desbloqueia_pullback_recuperacao_e_early_cross_com_ker_baixo():
    strat = RenderQuantV1()
    strat.dp = MagicMock()
    strat.dp.runmode = "backtest"

    # 1. Pullback em recuperacao acima da SMA50 ascendente (SMA50 ainda abaixo da SMA200)
    df_pb = pd.DataFrame({
        "open": [98.0 + i * 0.1 for i in range(10)],
        "high": [99.2 + i * 0.1 for i in range(10)],
        "low": [97.8 + i * 0.1 for i in range(10)],
        "close": [98.9 + i * 0.1 for i in range(10)],
        "volume": [1000.0] * 10,
        "sma50": [96.0 + i * 0.3 for i in range(10)],  # sma50 subindo (96 -> 98.7)
        "sma200": [100.0] * 10,  # sma50 < sma200, mas close=99.8 >= 98.5% * 100
        "ema9": [99.4] * 10,
        "ema21": [99.3] * 10,
        "rsi": [46.0 + i * 0.5 for i in range(10)],
        "natr": [0.8] * 10,
        "bb_lower": [97.0] * 10,
        "bb_middle": [99.0] * 10,
        "bb_upper": [101.0] * 10,
        "bb_pos": [0.5] * 10,
        "macd": [0.1] * 10,
        "macd_signal": [0.2] * 10,
        "macd_hist": [-0.1] * 10,
        "pivot_r1": [103.0] * 10,
        "ker": [0.35] * 10,
        "is_whale_vol": [0] * 10,
        "vol_modified_z": [0.2] * 10,
        "market_regime_bull": [0] * 10,
        "volume_usd_24h": [500_000.0] * 10,
        "stretch_atr": [0.5] * 10,
    })
    df_pb.loc[9, "low"] = 99.1  # tocou EMA21
    out_pb = strat.populate_entry_trend(df_pb, {"pair": "ETH/USDT"})
    assert out_pb.loc[9, "enter_long"] == 1

    # 2. Early cross com KER=0.20 (típico no candle exato da virada de fundo)
    df_cr = df_pb.copy()
    df_cr["sma50"] = [94.0] * 10
    df_cr["sma200"] = [108.0] * 10
    df_cr["ema9"] = [98.8] * 9 + [99.6]
    df_cr["ema21"] = [99.2] * 9 + [99.3]
    df_cr["macd"] = [0.2] * 10
    df_cr["macd_signal"] = [0.1] * 10
    df_cr["macd_hist"] = [0.1] * 10
    df_cr["ker"] = [0.20] * 10
    df_cr.loc[9, "close"] = 99.8
    out_cr = strat.populate_entry_trend(df_cr, {"pair": "SOL/USDT"})
    assert out_cr.loc[9, "enter_long"] == 1


def test_v4_adaptive_opera_abaixo_90pct_ema200_sem_morrer():
    """V4 estava zerada ha 2+ semanas por exigir close > 0.90*EMA200; deve aceitar ate ~0.80*EMA200."""
    strat = RenderQuantV4_Adaptive()
    strat.dp = MagicMock()
    strat.dp.runmode = "backtest"

    df = pd.DataFrame({
        "open": [86.0, 84.5, 83.2],
        "high": [87.0, 85.0, 85.5],
        "low": [84.0, 82.5, 82.0],
        "close": [85.0, 83.0, 84.8],  # -15.2% abaixo da EMA200 (100.0)
        "volume": [1000.0, 1200.0, 1500.0],
        "ema20": [87.0, 86.0, 85.5],
        "ema50": [90.0, 89.5, 89.0],
        "ema200": [100.0, 100.0, 100.0],
        "rsi": [34.0, 29.0, 35.0],
        "bb_pos": [0.20, 0.08, 0.14],
        "lower_wick_ratio": [0.20, 0.25, 0.30],
        "btc_no_crash": [True, True, True],
        "vol_usd_24h": [180_000.0, 180_000.0, 180_000.0],  # LINK liquidity
    })
    out = strat.populate_entry_trend(df, {"pair": "LINK/USDT"})
    assert out["enter_long"].iloc[-1] == 1


def test_state_store_nao_sobrescreve_neon_quando_db_falha(tmp_path):
    """Se db_get falhar por erro de rede no Neon, load_state NAO pode chamar db_set com o arquivo local."""
    state_store.clear_state_cache()
    local_file = tmp_path / "strategy_state.json"
    local_file.write_text('{"active_strategy": "RenderQuantV3_Defensive"}', encoding="utf-8")

    with (
        patch("user_data.state_store._connect", side_effect=RuntimeError("Neon connection timeout")),
        patch("user_data.state_store.db_set") as m_set,
    ):
        res = state_store.load_state(
            "strategy_state",
            [local_file],
            {"active_strategy": "RenderQuantV3_Defensive"},
            cache_ttl_secs=0.0,
        )
        assert res["active_strategy"] == "RenderQuantV3_Defensive"
        assert m_set.call_count == 0, "Nao deve sobrescrever o Neon DB quando o erro foi de conexao!"
