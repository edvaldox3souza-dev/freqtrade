import sys
from unittest.mock import MagicMock, patch
from datetime import datetime
import pathlib
import pytest

for mod in ["talib", "talib.abstract", "freqtrade", "freqtrade.persistence", "freqtrade.strategy"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

sys.modules["freqtrade.persistence"].Trade.get_trades_proxy = MagicMock(return_value=[])

class MockIStrategy:
    pass

sys.modules["freqtrade.strategy"].IStrategy = MockIStrategy

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from user_data.strategies.RenderQuantV3_Defensive import RenderQuantV3_Defensive
from user_data.strategies.RenderQuantV4_Adaptive import RenderQuantV4_Adaptive


def test_ai_guardian_disabled_passes():
    strat = RenderQuantV3_Defensive()
    strat.wallets = MagicMock()
    strat.wallets.get_total_stake_amount.return_value = 1000.0

    with patch("user_data.ai_guardian.get_state", return_value={"enabled": False}):
        ok = strat.confirm_trade_entry("BTC/USDT", "limit", 0.001, 95000.0, "GTC", datetime.now(), None, "long")
        assert ok is True


def test_ai_guardian_veto_blocks_entry():
    strat = RenderQuantV3_Defensive()
    strat.wallets = MagicMock()
    strat.wallets.get_total_stake_amount.return_value = 1000.0

    mock_state = {"enabled": True}
    mock_verdict = {"aprovado": False, "conviccao": 0.45, "motivo": "VETO: Risco excessivo de exaustão"}

    with patch("user_data.ai_guardian.get_state", return_value=mock_state):
        with patch("user_data.ai_guardian.consult_ai_guardian", return_value=mock_verdict):
            ok = strat.confirm_trade_entry("BTC/USDT", "limit", 0.001, 95000.0, "GTC", datetime.now(), None, "long")
            assert ok is False


def test_ai_guardian_approval_allows_entry():
    strat = RenderQuantV4_Adaptive()
    strat.wallets = MagicMock()
    strat.wallets.get_total_stake_amount.return_value = 1000.0

    mock_state = {"enabled": True}
    mock_verdict = {"aprovado": True, "conviccao": 0.85, "motivo": "Confluência de suporte aprovada"}

    with patch("user_data.ai_guardian.get_state", return_value=mock_state):
        with patch("user_data.ai_guardian.consult_ai_guardian", return_value=mock_verdict):
            ok = strat.confirm_trade_entry("SOL/USDT", "limit", 0.05, 180.0, "GTC", datetime.now(), None, "long")
            assert ok is True
