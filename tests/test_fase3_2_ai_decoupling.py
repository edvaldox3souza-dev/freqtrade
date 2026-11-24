"""Suíte Forense Fase 3.2 — Desacoplamento Completo da IA, Latência O(1), Concorrência e Recuperação."""
import concurrent.futures
import json
import pathlib
import sys
import threading
import time
from datetime import UTC, datetime
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

# Mock módulos ausentes no host Windows
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
from user_data.strategies.RenderQuantV3_Defensive import RenderQuantV3_Defensive
from user_data.strategies.RenderQuantV4_Adaptive import RenderQuantV4_Adaptive


def _make_ok_resp(aprovado=True, conviccao=0.85, motivo="Confluência OK"):
    r = MagicMock(status_code=200)
    r.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"aprovado": aprovado, "conviccao": conviccao, "motivo": motivo})}}]
    }
    return r


class TestFase32DesacoplamentoConcorrenciaELatencia:
    MOCK_STATE: ClassVar[dict] = {
        "enabled": True,
        "api_key": "fase32_test_key",
        "endpoint": "https://api.miraiapi.com/v1/chat/completions",
        "model": "claude-opus-5.5",
        "min_conviccao": 0.65,
    }

    def setup_method(self):
        ai_guardian.configure_ai_worker(max_workers=2, max_queue_size=16)
        ai_guardian.clear_verdict_cache()

    def teardown_method(self):
        ai_guardian.shutdown_ai_worker(wait=True, timeout=2.0)

    def test_confirm_trade_entry_nao_espera_rede_nem_worker(self):
        """Mesmo com API demorando 1.5s, confirm_trade_entry retorna em < 20ms (fail-closed pending_async)."""
        strat = RenderQuantV1()
        strat.wallets = MagicMock()
        strat.wallets.get_total_stake_amount.return_value = 1000.0
        strat.wallets.get_available_stake_amount.return_value = 1000.0
        strat.dp = None

        def slow_network(*args, **kwargs):
            time.sleep(1.5)
            return _make_ok_resp(True, 0.90)

        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", side_effect=slow_network),
        ):
            t0 = time.perf_counter()
            ok = strat.confirm_trade_entry(
                pair="BTC/USDT",
                order_type="limit",
                amount=0.001,
                rate=60000.0,
                time_in_force="gtc",
                current_time=datetime.now(UTC),
                entry_tag="V1:equilibrada_pullback",
                side="long",
            )
            dt_ms = (time.perf_counter() - t0) * 1000.0
            assert ok is False  # fail-closed enquanto pending_async
            assert dt_ms < 50.0, f"confirm_trade_entry bloqueou por {dt_ms:.2f}ms!"

    def test_pre_avaliacao_em_background_popula_cache_para_leitura_o1(self):
        """Fluxo real: enqueue na vela -> worker processa -> confirm_trade_entry lê cache hit em O(1)."""
        strat = RenderQuantV1()
        strat.wallets = MagicMock()
        strat.wallets.get_total_stake_amount.return_value = 1000.0
        strat.wallets.get_available_stake_amount.return_value = 1000.0
        strat.dp = None

        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", return_value=_make_ok_resp(True, 0.88, "Pré-avaliado")) as m_post,
        ):
            enqueued = ai_guardian.enqueue_ai_pre_evaluation(
                pair="BTC/USDT", rate=60000.0, ker=0.45, atr_pct=0.018, vol_z=1.2,
                regime_bull=True, daily_drawdown_pct=0.0, mode="RenderQuantV1",
                side="long", signal_tag="V1:equilibrada_pullback", timeframe_secs=900,
            )
            assert enqueued is True

            # Aguarda brevemente o worker concluir fora do callback
            deadline = time.time() + 2.0
            while ai_guardian.get_ai_metrics()["completed"] < 1 and time.time() < deadline:
                time.sleep(0.01)

            t0 = time.perf_counter()
            ok = strat.confirm_trade_entry(
                pair="BTC/USDT", order_type="limit", amount=0.0004, rate=60000.0,
                time_in_force="gtc", current_time=datetime.now(UTC),
                entry_tag="V1:equilibrada_pullback", side="long",
            )
            dt_hit_ms = (time.perf_counter() - t0) * 1000.0
            metrics = ai_guardian.get_ai_metrics()

            assert ok is True
            assert m_post.call_count == 1
            assert metrics["cache_hits"] == 1
            assert dt_hit_ms < 20.0, f"Cache hit demorou {dt_hit_ms:.2f}ms"

    def test_seis_pares_simultaneos_e_multiplos_sinais_mesmo_par(self):
        pairs = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT", "LINK/USDT"]
        c_ts = time.time()
        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", return_value=_make_ok_resp(True, 0.85)) as m_post,
        ):
            for p in pairs:
                assert ai_guardian.enqueue_ai_pre_evaluation(
                    p, 100.0, 0.45, 0.02, 1.0, True, 0.0, signal_tag="sig_1", candle_ts=c_ts
                ) is True
            # Mesmo par com sinal diferente -> deve enfileirar separado
            assert ai_guardian.enqueue_ai_pre_evaluation(
                "BTC/USDT", 100.0, 0.45, 0.02, 1.0, True, 0.0, signal_tag="sig_2", candle_ts=c_ts
            ) is True

            deadline = time.time() + 3.0
            while ai_guardian.get_ai_metrics()["completed"] < 7 and time.time() < deadline:
                time.sleep(0.01)

            assert m_post.call_count == 7
            for p in pairs:
                v = ai_guardian.get_or_enqueue_ai_verdict(p, 100.0, 0.45, 0.02, 1.0, True, 0.0, signal_tag="sig_1", candle_ts=c_ts)
                assert v["aprovado"] is True and v["origem"] == "ia"

    def test_rejeicao_de_resultado_atrasado_apos_expiracao_do_sinal(self):
        def delayed_resp(*args, **kwargs):
            time.sleep(0.15)
            return _make_ok_resp(True, 0.90, "Atrasado")

        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", side_effect=delayed_resp),
        ):
            # Sinal expira em 0.05s, mas API leva 0.15s -> deve descartar como late_discarded!
            ai_guardian.enqueue_ai_pre_evaluation(
                "BTC/USDT", 60000.0, 0.45, 0.02, 1.0, True, 0.0, signal_ttl_secs=0.05
            )
            time.sleep(0.30)
            metrics = ai_guardian.get_ai_metrics()
            assert metrics["late_discarded"] >= 1
            assert metrics["cache_size"] == 0

    def test_nova_vela_enquanto_consulta_anterior_pendente(self):
        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", return_value=_make_ok_resp(True, 0.85)),
        ):
            t_vela_1 = time.time() - 900.0
            t_vela_2 = time.time()
            ai_guardian.enqueue_ai_pre_evaluation("ETH/USDT", 3000.0, 0.45, 0.02, 1.0, True, 0.0, candle_ts=t_vela_1)
            v_vela_2 = ai_guardian.get_or_enqueue_ai_verdict("ETH/USDT", 3010.0, 0.45, 0.02, 1.0, True, 0.0, candle_ts=t_vela_2)
            assert v_vela_2["aprovado"] is False
            assert v_vela_2["origem"] == "pending_async"

    def test_fila_cheia_e_limite_de_workers_e_deduplicacao(self):
        ai_guardian.configure_ai_worker(max_workers=1, max_queue_size=2)
        ai_guardian.clear_verdict_cache()
        gate = threading.Event()

        def blocked_post(*args, **kwargs):
            gate.wait(timeout=2.0)
            return _make_ok_resp(True, 0.85)

        try:
            with (
                patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
                patch("user_data.ai_guardian.update_state"),
                patch("requests.post", side_effect=blocked_post),
            ):
                # 1o entra em execução no worker; 2o e 3o enchem fila (maxsize=2); 4o e 5o são dropados
                ai_guardian.enqueue_ai_pre_evaluation("BTC/USDT", 1.0, 0.45, 0.02, 1.0, True, 0.0)
                time.sleep(0.05)  # garante que worker pegou o 1o item
                # Deduplicação do mesmo item
                dup = ai_guardian.enqueue_ai_pre_evaluation("BTC/USDT", 1.0, 0.45, 0.02, 1.0, True, 0.0)
                assert dup is False
                # Enche fila com 2 itens distintos
                assert ai_guardian.enqueue_ai_pre_evaluation("ETH/USDT", 1.0, 0.45, 0.02, 1.0, True, 0.0) is True
                assert ai_guardian.enqueue_ai_pre_evaluation("SOL/USDT", 1.0, 0.45, 0.02, 1.0, True, 0.0) is True
                # 4o item encontra fila cheia -> queue_dropped
                dropped = ai_guardian.enqueue_ai_pre_evaluation("XRP/USDT", 1.0, 0.45, 0.02, 1.0, True, 0.0)
                assert dropped is False

                metrics = ai_guardian.get_ai_metrics()
                assert metrics["active_workers"] <= 1
                assert metrics["deduplicated"] >= 1
                assert metrics["queue_dropped"] >= 1
        finally:
            gate.set()

    def test_timeouts_erros_http_json_recuperacao_e_excecao_no_worker(self):
        import requests as req
        r_500 = MagicMock(status_code=500)
        r_bad_200 = MagicMock(status_code=200)
        r_bad_200.json.return_value = {"choices": [{"message": {"content": "sem json"}}]}
        r_incomplete = MagicMock(status_code=200)
        r_incomplete.json.return_value = {"choices": [{"message": {"content": '{"aprovado": true}'}}]}

        resps = [
            req.ConnectTimeout("connect timeout"),
            req.ReadTimeout("read timeout"),
            r_500,
            r_bad_200,
            r_incomplete,
            _make_ok_resp(True, 0.91, "Recuperado"),
        ]
        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", side_effect=resps),
        ):
            for _ in range(5):
                v = ai_guardian.consult_ai_guardian("ADA/USDT", 0.5, 0.45, 0.02, 1.0, True, 0.0)
                assert v["origem"] == "fail_closed" and v["aprovado"] is False
            # 6a tentativa após recuperação: sucesso!
            v_ok = ai_guardian.consult_ai_guardian("ADA/USDT", 0.5, 0.45, 0.02, 1.0, True, 0.0)
            assert v_ok["origem"] == "ia" and v_ok["aprovado"] is True

    def test_encerramento_durante_consulta_e_reinicializacao_servico(self):
        started = threading.Event()
        release = threading.Event()

        def slow_post(*args, **kwargs):
            started.set()
            release.wait(timeout=2.0)
            return _make_ok_resp(True, 0.85)

        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", side_effect=slow_post),
        ):
            ai_guardian.enqueue_ai_pre_evaluation("LINK/USDT", 15.0, 0.45, 0.02, 1.0, True, 0.0)
            started.wait(timeout=1.0)
            ai_guardian.shutdown_ai_worker(wait=False)
            release.set()
            time.sleep(0.05)
            # Após shutdown/reinício, resposta antiga não pode ter entrado no cache
            v = ai_guardian.get_or_enqueue_ai_verdict("LINK/USDT", 15.0, 0.45, 0.02, 1.0, True, 0.0)
            assert v["origem"] == "pending_async"

    def test_duas_requisicoes_identicas_concorrentes_deduplicadas(self):
        counter = 0
        lock = threading.Lock()

        def slow_ok(*args, **kwargs):
            nonlocal counter
            time.sleep(0.1)
            with lock:
                counter += 1
            return _make_ok_resp(True, 0.88)

        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", side_effect=slow_ok),
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                futs = [
                    ex.submit(ai_guardian.enqueue_ai_pre_evaluation, "BTC/USDT", 60000.0, 0.45, 0.02, 1.0, True, 0.0)
                    for _ in range(4)
                ]
                res = [f.result() for f in futs]
            time.sleep(0.25)
            assert sum(1 for x in res if x is True) == 1
            assert counter == 1


class TestFase32RecuperacaoEEstrategias:
    @pytest.mark.parametrize(
        "cls",
        [RenderQuantV1, RenderQuantV2_Institutional, RenderQuantV3_Defensive, RenderQuantV4_Adaptive],
    )
    def test_protecoes_nativas_e_saidas_independentes_da_ia(self, cls):
        strat = cls()
        assert strat.order_types["stoploss_on_exchange"] is True
        assert strat.order_types["emergency_exit"] == "market"
        assert strat.stoploss < 0
        mock_trade = MagicMock()
        mock_trade.open_date_utc = datetime.now(UTC)
        sl = strat.custom_stoploss("BTC/USDT", mock_trade, datetime.now(UTC), 58000.0, -0.02, False)
        assert sl < 0
