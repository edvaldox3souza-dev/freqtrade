"""Suíte Adversarial Fase 3.3 — Backtesting/Hyperopt, Concorrência 100x, Identidade e Segurança."""
import concurrent.futures
import json
import pathlib
import sys
import threading
import time
from datetime import UTC, datetime
from typing import ClassVar
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

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


def _ok_resp(aprovado=True, conviccao=0.90, motivo="OK"):
    r = MagicMock(status_code=200)
    r.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"aprovado": aprovado, "conviccao": conviccao, "motivo": motivo})}}]
    }
    return r


class TestFase33BacktestHyperoptZeroIA:
    @pytest.mark.parametrize("runmode", ["backtest", "hyperopt"])
    @pytest.mark.parametrize("cls", [RenderQuantV1, RenderQuantV2_Institutional, RenderQuantV3_Defensive, RenderQuantV4_Adaptive])
    def test_backtest_e_hyperopt_nao_disparam_ia(self, cls, runmode):
        ai_guardian.clear_verdict_cache()
        strat = cls()
        strat.dp = MagicMock()
        strat.dp.runmode = runmode
        strat.dp.get_analyzed_dataframe.return_value = (pd.DataFrame(), datetime.now(UTC))
        strat.wallets = MagicMock()
        strat.wallets.get_total_stake_amount.return_value = 1000.0
        strat.wallets.get_available_stake_amount.return_value = 1000.0

        with (
            patch("user_data.ai_guardian.enqueue_ai_pre_evaluation") as m_enq,
            patch("user_data.ai_guardian.get_or_enqueue_ai_verdict") as m_get,
            patch("requests.post") as m_post,
        ):
            # confirm_trade_entry em backtest/hyperopt não deve chamar IA
            ok = strat.confirm_trade_entry("BTC/USDT", "limit", 0.0005, 60000.0, "gtc", datetime.now(UTC), "tag", "long")
            assert ok is True
            assert m_enq.call_count == 0
            assert m_get.call_count == 0
            assert m_post.call_count == 0

            # populate_entry_trend em V3 com sinal não deve enfileirar IA em backtest/hyperopt
            if cls is RenderQuantV3_Defensive:
                df = pd.DataFrame({
                    "close": [100.0, 100.0],
                    "volume": [1000.0, 1000.0],
                    "bb_pos": [0.5, 0.05],
                    "ema200": [90.0, 90.0],
                    "vol_usd_24h": [5_000_000.0, 5_000_000.0],
                    "atr_pct": [0.02, 0.02],
                })
                strat.populate_entry_trend(df, {"pair": "BTC/USDT"})
                assert m_enq.call_count == 0
                assert m_post.call_count == 0


class TestFase33ConcorrenciaExtremaESeguranca:
    MOCK_STATE: ClassVar[dict] = {
        "enabled": True,
        "api_key": "sk-secret-do-not-leak-1234567890",
        "endpoint": "https://api.miraiapi.com/v1/chat/completions",
        "model": "claude-opus-5.5",
        "min_conviccao": 0.65,
    }

    def setup_method(self):
        ai_guardian.configure_ai_worker(max_workers=2, max_queue_size=16)
        ai_guardian.clear_verdict_cache()

    def teardown_method(self):
        ai_guardian.shutdown_ai_worker(wait=True, timeout=2.0)

    def test_100_solicitacoes_simultaneas_mesmo_sinal_sem_deadlock(self):
        counter = 0
        lock = threading.Lock()

        def slow_ok(*args, **kwargs):
            nonlocal counter
            time.sleep(0.05)
            with lock:
                counter += 1
            return _ok_resp(True, 0.88)

        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", side_effect=slow_ok),
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
                futs = [
                    ex.submit(ai_guardian.enqueue_ai_pre_evaluation, "BTC/USDT", 60000.0, 0.45, 0.02, 1.0, True, 0.0)
                    for _ in range(100)
                ]
                results = [f.result(timeout=5.0) for f in futs]

            time.sleep(0.15)
            metrics = ai_guardian.get_ai_metrics()
            assert sum(1 for r in results if r is True) == 1
            assert counter == 1
            assert metrics["active_workers"] <= 2
            assert metrics["deduplicated"] >= 99

    def test_100_sinais_distintos_respeitam_fila_limitada_sem_orfaos(self):
        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", return_value=_ok_resp(True, 0.88)),
        ):
            for i in range(100):
                ai_guardian.enqueue_ai_pre_evaluation(
                    f"PAIR{i}/USDT", 100.0, 0.45, 0.02, 1.0, True, 0.0, signal_tag=f"sig_{i}"
                )
            deadline = time.time() + 3.0
            while ai_guardian.get_ai_metrics()["in_flight"] > 0 and time.time() < deadline:
                time.sleep(0.01)

            m = ai_guardian.get_ai_metrics()
            assert m["queue_size"] == 0
            assert m["in_flight"] == 0
            assert (m["completed"] + m["queue_dropped"] + m["late_discarded"]) >= 100

    def test_identidade_lado_e_estrategia_diferentes(self):
        c_ts = time.time()
        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", return_value=_ok_resp(True, 0.90)),
        ):
            ai_guardian.enqueue_ai_pre_evaluation(
                "BTC/USDT", 60000.0, 0.45, 0.02, 1.0, True, 0.0,
                mode="RenderQuantV1", side="long", signal_tag="s1", candle_ts=c_ts,
            )
            deadline = time.time() + 2.0
            while ai_guardian.get_ai_metrics()["completed"] < 1 and time.time() < deadline:
                time.sleep(0.01)

            # Lado diferente -> NÃO pode usar veredicto de long
            v_short = ai_guardian.get_or_enqueue_ai_verdict(
                "BTC/USDT", 60000.0, 0.45, 0.02, 1.0, True, 0.0,
                mode="RenderQuantV1", side="short", signal_tag="s1", candle_ts=c_ts,
            )
            # Estratégia diferente -> NÃO pode usar veredicto da V1
            v_v2 = ai_guardian.get_or_enqueue_ai_verdict(
                "BTC/USDT", 60000.0, 0.45, 0.02, 1.0, True, 0.0,
                mode="RenderQuantV2_Institutional", side="long", signal_tag="s1", candle_ts=c_ts,
            )
            assert v_short["aprovado"] is False and v_short["origem"] == "pending_async"
            assert v_v2["aprovado"] is False and v_v2["origem"] == "pending_async"

    def test_seguranca_nao_vaza_api_key_e_ignora_injecao_campos(self):
        injected = MagicMock(status_code=200)
        injected.json.return_value = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "aprovado": True,
                        "conviccao": 0.95,
                        "motivo": "OK",
                        "stake_amount": 999999,
                        "stoploss": 0.0,
                        "execute_order": True,
                    })
                }
            }]
        }
        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", return_value=injected),
        ):
            v = ai_guardian.consult_ai_guardian("BTC/USDT", 60000.0, 0.45, 0.02, 1.0, True, 0.0)
            assert "sk-secret-do-not-leak" not in json.dumps(v)
            assert "stake_amount" not in v
            assert "stoploss" not in v
            assert "execute_order" not in v
            assert np.isfinite(v["conviccao"])
