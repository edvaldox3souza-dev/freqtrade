import sys
import pathlib
import pytest
import numpy as np
import pandas as pd
from datetime import datetime
from unittest.mock import patch, MagicMock

# Mock de dependências externas para testes unitários
for mod in ["talib", "talib.abstract", "freqtrade", "freqtrade.persistence", "freqtrade.strategy"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

class MockIStrategy:
    pass

sys.modules["freqtrade.strategy"].IStrategy = MockIStrategy

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from user_data.strategies.RenderQuantV4_Adaptive import RenderQuantV4_Adaptive


def test_full_pipeline_v4_entry_gate_and_ai_arbitration():
    """Valida formalmente todas as regras de negócio de ponta a ponta."""
    strat = RenderQuantV4_Adaptive()
    strat.dp = MagicMock()

    # 1. Simula candles onde ocorre exatamente o padrão de entrada quantitativa
    df = pd.DataFrame({
        "open": [100.0, 95.0, 90.0, 88.0],
        "high": [102.0, 96.0, 91.0, 92.0],
        "low":  [98.0,  92.0, 85.0, 84.0],  # Vela 3 teve pavio longo inferior (88 - 84 = 4)
        "close": [99.0,  93.0, 89.0, 91.0],  # Fechou em 91.0 (reversão verde)
        "volume": [1000.0, 1200.0, 2500.0, 3000.0],
    })

    # Indicadores técnicos pré-calculados
    df["bb_lowerband"] = [95.0, 90.0, 88.0, 87.0]
    df["bb_upperband"] = [105.0, 102.0, 100.0, 98.0]
    df["bb_pos"] = [0.40, 0.25, 0.10, 0.05]  # Última vela em 0.05 (abaixo de bb_pos_entry = 0.08)
    df["ema200"] = [90.0, 90.0, 90.0, 90.0]  # Close (91) > EMA200 * 0.90 (81.0)
    df["ema50"] = [92.0, 92.0, 92.0, 92.0]
    df["candle_range"] = df["high"] - df["low"]
    df["lower_wick"] = np.minimum(df["open"], df["close"]) - df["low"]
    df["lower_wick_ratio"] = df["lower_wick"] / df["candle_range"]
    df["btc_no_crash"] = [True, True, True, True]
    df["vol_usd_24h"] = [5_000_000.0, 5_000_000.0, 5_000_000.0, 5_000_000.0]
    df["rsi"] = [45.0, 38.0, 32.0, 35.0]

    # Popula sinal de entrada
    df = strat.populate_entry_trend(df, {"pair": "BTC/USDT"})
    assert "enter_long" in df.columns
    # A última vela deve ter gerado sinal de entrada (enter_long == 1)
    assert df["enter_long"].iloc[-1] == 1

    # 2. Teste do Portão Cognitivo (AI Guardian Gate):
    strat.dp = None  # Sem DataProvider externo no teste unitário
    # Cenário A: Claude Opus 5.5 aprova a entrada (convicção 82%) via leitura O(1)
    with patch("user_data.ai_guardian.get_state", return_value={"enabled": True}), \
         patch("user_data.ai_guardian.get_or_enqueue_ai_verdict") as mock_ai:
        mock_ai.return_value = {
            "aprovado": True,
            "conviccao": 0.82,
            "motivo": "Reversão clara com pavio de absorção",
            "origem": "ia",
        }
        approved = strat.confirm_trade_entry(
            pair="BTC/USDT",
            order_type="limit",
            amount=0.00016,
            rate=91000.0,
            time_in_force="gtc",
            current_time=datetime.now(),
            entry_tag="bollinger_mean_reversion",
            side="long",
        )
        assert approved is True
        mock_ai.assert_called_once()

    # Cenário B: Claude Opus 5.5 VETA a entrada por risco macro
    with patch("user_data.ai_guardian.get_state", return_value={"enabled": True}), \
         patch("user_data.ai_guardian.get_or_enqueue_ai_verdict") as mock_ai:
        mock_ai.return_value = {
            "aprovado": False,
            "conviccao": 0.42,
            "motivo": "Volume Z-score indicando manipulação de topo local",
            "origem": "ia",
        }
        rejected = strat.confirm_trade_entry(
            pair="BTC/USDT",
            order_type="limit",
            amount=0.00016,
            rate=91000.0,
            time_in_force="gtc",
            current_time=datetime.now(),
            entry_tag="bollinger_mean_reversion",
            side="long",
        )
        assert rejected is False


def test_v4_exit_on_profit_exhaustion():
    strat = RenderQuantV4_Adaptive()
    df = pd.DataFrame({
        "open": [90.0, 95.0, 100.0],
        "high": [92.0, 98.0, 104.0],
        "low":  [89.0, 94.0, 99.0],
        "close": [91.0, 97.0, 103.0],
        "volume": [1000.0, 1500.0, 2000.0],
        "bb_pos": [0.20, 0.60, 0.85],  # Última vela atinge banda superior (> 0.80)
        "rsi": [40.0, 55.0, 72.0],     # RSI sobrecomprado (> 70)
    })

    df = strat.populate_exit_trend(df, {"pair": "BTC/USDT"})
    assert "exit_long" in df.columns
    # Última vela deve gerar sinal de saída com lucro no topo
    assert df["exit_long"].iloc[-1] == 1
