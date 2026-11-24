import sys
from unittest.mock import MagicMock
from datetime import datetime
import pathlib
import pytest

# Mock talib e freqtrade para execução de testes locais no Windows
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


def test_v4_custom_stoploss_logic():
    strat = RenderQuantV4_Adaptive()
    mock_trade = MagicMock()

    # Cenário 1: Trade recém aberto (lucro de +1.0% ou negativo -2.0%) -> Stop fixo -5.0%
    sl_initial = strat.custom_stoploss("BTC/USDT", mock_trade, datetime.now(), 95000.0, 0.010, False)
    assert sl_initial == -0.050

    sl_negative = strat.custom_stoploss("BTC/USDT", mock_trade, datetime.now(), 93000.0, -0.020, False)
    assert sl_negative == -0.050

    # Cenário 2: Trade atingiu +3.0% de lucro -> Breakeven ativado (+0.6% de lucro garantido)
    # Se lucro = +0.030, a distância até o ponto de entrada + 0.006 é: -(0.030 - 0.006) = -0.024
    sl_be = strat.custom_stoploss("BTC/USDT", mock_trade, datetime.now(), 97000.0, 0.030, False)
    assert pytest.approx(sl_be, 0.0001) == -(0.030 - 0.006)

    # Cenário 3: Trade atingiu +8.0% de lucro (super tendência) -> Trailing Stop de 2.0%
    sl_trail = strat.custom_stoploss("BTC/USDT", mock_trade, datetime.now(), 102000.0, 0.080, False)
    assert sl_trail == -0.020


def test_v4_stake_micro_wallet():
    strat = RenderQuantV4_Adaptive()
    strat.wallets = MagicMock()

    # Conta normal ($1000)
    strat.wallets.get_total_stake_amount.return_value = 1000.0
    stake = strat.custom_stake_amount("ETH/USDT", datetime.now(), 3400.0, 100.0, 6.80, 9999.0, 1.0, None, "long")
    assert stake == 100.0

    # Micro-banca ($16.29) com min_stake da exchange ($6.80) -> DEVE recusar (0.0) por violar cap 10% ($1.63) e risco 1%
    strat.wallets.get_total_stake_amount.return_value = 16.29
    stake_micro = strat.custom_stake_amount("ETH/USDT", datetime.now(), 3400.0, 1.62, 6.80, 9999.0, 1.0, None, "long")
    assert stake_micro == 0.0
