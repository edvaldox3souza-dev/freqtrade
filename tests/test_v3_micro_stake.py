import sys
from unittest.mock import MagicMock
from datetime import datetime
import pathlib
import pytest

# Mock talib e freqtrade se ausentes no ambiente de teste local
for mod in ["talib", "talib.abstract", "freqtrade", "freqtrade.persistence", "freqtrade.strategy"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

class MockIStrategy:
    pass

sys.modules["freqtrade.strategy"].IStrategy = MockIStrategy

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from user_data.strategies.RenderQuantV3_Defensive import RenderQuantV3_Defensive


def test_v3_stake_calculation():
    strat = RenderQuantV3_Defensive()
    strat.wallets = MagicMock()
    
    # Caso 1: Conta institucional ($1000) -> 10% ($100.0)
    strat.wallets.get_total_stake_amount.return_value = 1000.0
    stake = strat.custom_stake_amount(
        pair="BTC/USDT",
        current_time=datetime.now(),
        current_rate=95000.0,
        proposed_stake=100.0,
        min_stake=5.70,
        max_stake=9999.0,
        leverage=1.0,
        entry_tag=None,
        side="long",
    )
    assert stake == 100.0, f"Esperado 100.0 para banca de 1000, obteve {stake}"

    # Caso 2: Micro-banca ($16.29) com min_stake de exchange ($5.70) -> DEVE liberar ($5.70 <= 60% = $9.77 e stop <= 5%)
    strat.wallets.get_total_stake_amount.return_value = 16.29
    stake_micro = strat.custom_stake_amount(
        pair="BTC/USDT",
        current_time=datetime.now(),
        current_rate=95000.0,
        proposed_stake=1.62,
        min_stake=5.70,
        max_stake=16.12,
        leverage=1.0,
        entry_tag=None,
        side="long",
    )
    assert stake_micro == 5.70, f"Esperado 5.70 para micro-banca liberada, obteve {stake_micro}"

    # Caso 3: Micro-banca ($16.29) com min_stake inalcançável ($25.00) -> 0.0 (seguro)
    stake_excess = strat.custom_stake_amount(
        pair="BTC/USDT",
        current_time=datetime.now(),
        current_rate=95000.0,
        proposed_stake=1.62,
        min_stake=25.00,
        max_stake=9999.0,
        leverage=1.0,
        entry_tag=None,
        side="long",
    )
    assert stake_excess == 0.0, f"Esperado 0.0 para min_stake maior que a carteira, obteve {stake_excess}"
