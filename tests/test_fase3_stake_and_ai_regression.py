import pathlib
import sys
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

# Mock módulos ausentes se necessário
for mod in ["talib", "talib.abstract", "freqtrade", "freqtrade.persistence", "freqtrade.strategy"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

class MockIStrategy:
    pass

sys.modules["freqtrade.strategy"].IStrategy = MockIStrategy

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from user_data import ai_guardian
from user_data.strategies.RenderQuantV1 import RenderQuantV1
from user_data.strategies.RenderQuantV2_Institutional import RenderQuantV2_Institutional


class TestStakePolicyV1V2V3V4:
    """Validação da política de stake conforme a Regra 2."""

    def test_v1_institutional_wallet(self):
        strat = RenderQuantV1()
        strat.wallets = MagicMock()
        strat.wallets.get_total_stake_amount.return_value = 1000.0
        stake = strat.custom_stake_amount(
            pair="BTC/USDT", current_time=datetime.now(), current_rate=60000.0,
            proposed_stake=100.0, min_stake=5.0, max_stake=9999.0, leverage=1.0, entry_tag=None, side="long"
        )
        assert stake == 100.0, f"Esperado 100.0 (10% de 1000), obteve {stake}"

    def test_v1_micro_wallet_kraken_ada(self):
        strat = RenderQuantV1()
        strat.wallets = MagicMock()
        strat.wallets.get_total_stake_amount.return_value = 16.30
        stake = strat.custom_stake_amount(
            pair="ADA/USDT", current_time=datetime.now(), current_rate=0.1465,
            proposed_stake=1.63, min_stake=3.76, max_stake=9999.0, leverage=1.0, entry_tag=None, side="long"
        )
        assert stake == 3.76, f"Esperado 3.76 para cobrir min_stake na micro-banca, obteve {stake}"

    def test_v1_micro_wallet_kraken_btc(self):
        strat = RenderQuantV1()
        strat.wallets = MagicMock()
        strat.wallets.get_total_stake_amount.return_value = 16.30
        stake = strat.custom_stake_amount(
            pair="BTC/USDT", current_time=datetime.now(), current_rate=60000.0,
            proposed_stake=1.63, min_stake=4.85, max_stake=9999.0, leverage=1.0, entry_tag=None, side="long"
        )
        assert stake == 4.85, f"Esperado 4.85 para cobrir min_stake BTC, obteve {stake}"

    def test_v1_micro_wallet_insufficient_for_min_stake(self):
        strat = RenderQuantV1()
        strat.wallets = MagicMock()
        strat.wallets.get_total_stake_amount.return_value = 16.30
        # min_stake de $12.00 excede a cota de segurança do slot (max 40% da banca = $6.52)
        stake = strat.custom_stake_amount(
            pair="EXPENSIVE/USDT", current_time=datetime.now(), current_rate=100.0,
            proposed_stake=1.63, min_stake=12.00, max_stake=9999.0, leverage=1.0, entry_tag=None, side="long"
        )
        assert stake == 0.0, f"Esperado 0.0 para min_stake que excede limite de segurança, obteve {stake}"

    def test_v1_min_stake_none_handles_cleanly(self):
        strat = RenderQuantV1()
        strat.wallets = MagicMock()
        strat.wallets.get_total_stake_amount.return_value = 1000.0
        stake = strat.custom_stake_amount(
            pair="BTC/USDT", current_time=datetime.now(), current_rate=60000.0,
            proposed_stake=100.0, min_stake=None, max_stake=9999.0, leverage=1.0, entry_tag=None, side="long"
        )
        assert stake > 0, "Deveria calcular normalmente com min_stake=None"

    def test_v1_max_stake_lower_than_min_stake(self):
        strat = RenderQuantV1()
        strat.wallets = MagicMock()
        strat.wallets.get_total_stake_amount.return_value = 1000.0
        stake = strat.custom_stake_amount(
            pair="BTC/USDT", current_time=datetime.now(), current_rate=60000.0,
            proposed_stake=100.0, min_stake=10.0, max_stake=5.0, leverage=1.0, entry_tag=None, side="long"
        )
        assert stake == 0.0, "Deveria retornar 0.0 se max_stake < min_stake"

    def test_v2_institutional_micro_wallet_parity(self):
        strat = RenderQuantV2_Institutional()
        strat.wallets = MagicMock()
        strat.wallets.get_total_stake_amount.return_value = 16.30
        stake = strat.custom_stake_amount(
            pair="SOL/USDT", current_time=datetime.now(), current_rate=70.0,
            proposed_stake=1.30, min_stake=3.70, max_stake=9999.0, leverage=1.0, entry_tag=None, side="long"
        )
        assert stake == 3.70, f"Esperado 3.70 na V2 para cobrir min_stake, obteve {stake}"


class TestAIGuardianResilience:
    """Validação da resiliência, latência e isolamento do Guardião IA conforme a Regra 3."""

    def test_ai_timeout_returns_fail_closed_without_hanging(self):
        mock_state = {
            "enabled": True,
            "api_key": "test_key_dummy_12345",
            "endpoint": "https://api.miraiapi.com/v1/chat/completions",
            "model": "claude-opus-5.5",
            "min_conviccao": 0.65,
        }
        import requests
        with (
            patch("user_data.ai_guardian.get_state", return_value=mock_state),
            patch("requests.post", side_effect=requests.Timeout("Connection timed out")),
        ):
            t0 = time.time()
            res = ai_guardian.consult_ai_guardian(
                pair="BTC/USDT", rate=60000.0, ker=0.45, atr_pct=0.018, vol_z=1.2,
                regime_bull=True, daily_drawdown_pct=0.0, mode="RenderQuantV1"
            )
            dt = time.time() - t0
            assert dt < 5.0, f"A chamada deveria retornar rapidamente no timeout, levou {dt}s"
            assert res["aprovado"] is False
            assert res["origem"] == "fail_closed"

    def test_ai_http_500_error_handling(self):
        mock_state = {
            "enabled": True,
            "api_key": "test_key",
            "endpoint": "https://api.miraiapi.com/v1/chat/completions",
            "model": "claude-opus-5.5",
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        with (
            patch("user_data.ai_guardian.get_state", return_value=mock_state),
            patch("requests.post", return_value=mock_resp),
        ):
            res = ai_guardian.consult_ai_guardian(
                pair="ETH/USDT", rate=1600.0, ker=0.40, atr_pct=0.02, vol_z=1.0,
                regime_bull=True, daily_drawdown_pct=0.0
            )
            assert res["aprovado"] is False
            assert "fail_closed" in res["origem"]

    def test_ai_invalid_json_handling(self):
        mock_state = {
            "enabled": True,
            "api_key": "test_key",
            "endpoint": "https://api.miraiapi.com/v1/chat/completions",
            "model": "claude-opus-5.5",
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Olá, sou a IA mas não vou retornar JSON válido!"}}]
        }
        with (
            patch("user_data.ai_guardian.get_state", return_value=mock_state),
            patch("requests.post", return_value=mock_resp),
        ):
            res = ai_guardian.consult_ai_guardian(
                pair="SOL/USDT", rate=70.0, ker=0.40, atr_pct=0.02, vol_z=1.0,
                regime_bull=True, daily_drawdown_pct=0.0
            )
            assert res["aprovado"] is False
            assert "fail_closed" in res["origem"]

    def test_ai_exit_failure_never_blocks_exit(self):
        mock_state = {
            "enabled": True,
            "enable_ai_exits": True,
            "api_key": "test_key",
            "endpoint": "https://api.miraiapi.com/v1/chat/completions",
            "model": "claude-opus-5.5",
        }
        with (
            patch("user_data.ai_guardian.get_state", return_value=mock_state),
            patch("requests.post", side_effect=Exception("API Crash")),
        ):
            res = ai_guardian.consult_ai_exit(
                pair="LINK/USDT", current_profit_pct=0.02, open_duration_min=120.0,
                ker=0.40, atr_pct=0.015, regime_bull=True
            )
            # Na saída, a falha NUNCA força saída nem quebra: retorna fallback passivo
            assert res["sair"] is False
            assert res["origem"] == "fallback"
