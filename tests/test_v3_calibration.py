"""Bateria de Testes Automatizados para RenderQuantV3_Defensive e start_bot.py.

Verifica:
1. Parametros calibrados para a liquidez real da Kraken Spot USDT.
2. Logica de Custom Stoploss (Breakeven dinamico e Trailing Stop).
3. Geracao precisa de sinais de entrada e saida.
4. Ausencia total de chamadas a pin_chat_message e presenca de unpin_all_chat_messages.
"""
import pathlib
import sys
from unittest import mock

import pandas as pd
import pytest

# Mock de dependencias do Freqtrade/talib para execucao direta no runner de testes
repo_root = pathlib.Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

mock_talib = mock.MagicMock()
sys.modules.setdefault("talib", mock_talib)
sys.modules.setdefault("talib.abstract", mock_talib)

mock_strat = mock.MagicMock()
class BaseStrategy:
    pass
mock_strat.IStrategy = BaseStrategy
sys.modules.setdefault("freqtrade", mock.MagicMock())
sys.modules.setdefault("freqtrade.persistence", mock.MagicMock())
sys.modules.setdefault("freqtrade.strategy", mock_strat)

from user_data.strategies.RenderQuantV3_Defensive import RenderQuantV3_Defensive


@pytest.fixture
def strategy():
    return RenderQuantV3_Defensive()


class TestRenderQuantV3Calibration:
    def test_parameters_calibration(self, strategy):
        """Verifica se os parametros operacionais estao exatamente calibrados para a Kraken."""
        assert strategy.timeframe == "4h"
        assert strategy.process_only_new_candles is False
        assert strategy.use_custom_stoploss is True
        assert strategy.bb_pos_entry == 0.12
        assert strategy.ema200_floor == 0.82
        assert strategy.min_vol_usd_24h == 200_000.0
        assert strategy.stoploss == -0.05
        assert strategy.order_types.get("stoploss_on_exchange") is True
        assert strategy.order_types.get("stoploss_on_exchange_interval") == 60

    def test_custom_stoploss_initial_loss(self, strategy):
        """Verifica se trades no prejuizo ou com lucro menor que 2.5% mantem o stop inicial de -5%."""
        mock_trade = mock.MagicMock()
        # Prejuizo de -2.0%
        sl = strategy.custom_stoploss("BTC/USDT", mock_trade, None, 60000.0, -0.02, False)
        assert sl == -0.05

        # Lucro pequeno de +1.5% (abaixo do breakeven de +2.5%)
        sl = strategy.custom_stoploss("BTC/USDT", mock_trade, None, 61000.0, 0.015, False)
        assert sl == -0.05

    def test_custom_stoploss_breakeven_lock(self, strategy):
        """Verifica se trades entre +2.5% e +5.0% travam o lucro liquido minimo de +0.6% (cobrindo taxas)."""
        mock_trade = mock.MagicMock()
        # Lucro de exatamente +2.5%: move stop para -(0.025 - 0.006) = -0.019
        sl_25 = strategy.custom_stoploss("BTC/USDT", mock_trade, None, 61500.0, 0.025, False)
        assert round(sl_25, 4) == -0.019

        # Lucro de +4.0%: move stop para -(0.040 - 0.006) = -0.034
        sl_40 = strategy.custom_stoploss("BTC/USDT", mock_trade, None, 62400.0, 0.040, False)
        assert round(sl_40, 4) == -0.034

    def test_custom_stoploss_trailing_profit(self, strategy):
        """Verifica se trades com lucro >= +5.0% ativam trailing stop de 2.0%."""
        mock_trade = mock.MagicMock()
        # Lucro de +5.0%
        sl_50 = strategy.custom_stoploss("BTC/USDT", mock_trade, None, 63000.0, 0.050, False)
        assert sl_50 == -0.020

        # Lucro de +12.0%
        sl_12 = strategy.custom_stoploss("BTC/USDT", mock_trade, None, 67200.0, 0.120, False)
        assert sl_12 == -0.020

    def test_entry_trend_signal_generation(self, strategy):
        """Verifica geracao de sinal de compra com confluencia dos filtros calibrados."""
        df = pd.DataFrame({
            "close": [100.0, 95.0, 70.0, 95.0],
            "ema200": [100.0, 100.0, 100.0, 100.0],
            "bb_pos": [0.50, 0.10, 0.10, 0.10],
            "vol_usd_24h": [500_000.0, 350_000.0, 350_000.0, 150_000.0],
            "volume": [100.0, 100.0, 100.0, 100.0],
        })

        df_out = strategy.populate_entry_trend(df.copy(), {"pair": "ETH/USDT"})
        enter_long = df_out.get("enter_long", pd.Series([0]*len(df)))

        # Linha 0: bb_pos 0.50 (meio da banda) -> Sem entrada
        assert enter_long.iloc[0] != 1

        # Linha 1: bb_pos 0.10 (< 0.12), close 95 > ema200*0.82 (82.0), vol 350k (> 300k) -> ENTRADA CONFIRMADA!
        assert enter_long.iloc[1] == 1
        assert df_out["enter_tag"].iloc[1] == "V3:defensive_mr"

        # Linha 2: bb_pos 0.10, mas close 70 < 82.0 (violou piso EMA200) -> Sem entrada (protegido)
        assert enter_long.iloc[2] != 1

        # Linha 3: bb_pos 0.10, close 95, mas vol 150k < 300k (falta liquidez) -> Sem entrada (protegido)
        assert enter_long.iloc[3] != 1

    def test_exit_trend_signal_generation(self, strategy):
        """Verifica geracao de saida por regressao a media ou sobrecompra."""
        df = pd.DataFrame({
            "bb_pos": [0.50, 0.78, 0.40],
            "rsi": [50.0, 55.0, 72.0],
            "volume": [100.0, 100.0, 100.0],
        })

        df_out = strategy.populate_exit_trend(df.copy(), {"pair": "SOL/USDT"})
        exit_long = df_out.get("exit_long", pd.Series([0]*len(df)))

        # Linha 0: neutro -> Sem saida
        assert exit_long.iloc[0] != 1

        # Linha 1: bb_pos 0.78 (> 0.75) -> SAIDA CONFIRMADA
        assert exit_long.iloc[1] == 1

        # Linha 2: rsi 72.0 (> 68.0) -> SAIDA CONFIRMADA
        assert exit_long.iloc[2] == 1


class TestTelegramNoPinnedMessages:
    def test_no_pin_chat_message_in_start_bot(self):
        """Garante que start_bot.py nao faz chamada para fixar mensagens no Telegram."""
        start_bot_path = repo_root / "start_bot.py"
        content = start_bot_path.read_text(encoding="utf-8")
        assert "bot.pin_chat_message" not in content
        assert " pin_chat_message" not in content

    def test_unpin_all_chat_messages_present(self):
        """Garante que start_bot.py possui chamada explicita para desfixar mensagens."""
        start_bot_path = repo_root / "start_bot.py"
        content = start_bot_path.read_text(encoding="utf-8")
        assert "unpin_all_chat_messages" in content


class TestBotRobustnessAndNoFakes:
    def test_get_target_message_helper_present(self):
        """Garante que o helper _get_target_message esta presente para proteger callbacks contra NoneType."""
        start_bot_path = repo_root / "start_bot.py"
        content = start_bot_path.read_text(encoding="utf-8")
        assert "def _get_target_message(update):" in content
        assert "_cmd_painel_web" in content
        assert "🌐 Painel Web" in content

    def test_telemetry_saves_to_tmp(self, strategy, tmp_path):
        """Verifica se _record_telemetry e resiliente e escreve no /tmp/why_state.json."""
        df = pd.DataFrame({
            "close": [60000.0],
            "bb_pos": [0.45],
            "rsi": [52.0],
            "ema200": [70000.0],
            "vol_usd_24h": [400_000.0],
            "enter_long": [0],
        })
        strategy._record_telemetry(df, "BTC/USDT")
        tmp_why = pathlib.Path("/tmp/why_state.json")
        # Se estiver em ambiente onde /tmp existe, o arquivo deve ser gravado
        if pathlib.Path("/tmp").exists():
            assert tmp_why.exists()
            content = tmp_why.read_text(encoding="utf-8")
            assert "BTC/USDT" in content

    def test_api_server_creds_persistence_structure(self):
        """Garante que _harden_environment possui rotina de persistencia estavel no state_store."""
        start_bot_path = repo_root / "start_bot.py"
        content = start_bot_path.read_text(encoding="utf-8")
        assert 'load_state("api_server_creds"' in content
        assert 'save_state("api_server_creds"' in content

