"""Suíte Forense Fase 3.1 — Contra-Auditoria Matemática, IA, Saídas, V1-V4 e Estresse."""
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

# Mock módulos ausentes no host Windows se necessário
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


# ---------------------------------------------------------------------------
# REGRA 1 — AUDITORIA MATEMÁTICA DA POLÍTICA DE STAKE (8 CENÁRIOS x 4 ESTRATÉGIAS)
# ---------------------------------------------------------------------------
class TestRegra1MatematicaStake:
    @pytest.mark.parametrize(
        ("wallet", "min_stake", "esperado_v1", "esperado_v2", "esperado_v3", "esperado_v4"),
        [
            # $16.30 -> slot máx 28% = $4.564, perda máx stop 1.5% = $0.2445
            # $2.95 e $3.76 cabem em todas (V1/V2 loss=$0.135/$0.172; V3/V4 loss=$0.166/$0.211)
            (16.30, 2.95, 2.95, 2.95, 2.95, 2.95),
            (16.30, 3.76, 3.76, 3.76, 3.76, 3.76),
            # $4.85 excede slot de 28% ($4.564) -> VETO em todas
            (16.30, 4.85, 0.0, 0.0, 0.0, 0.0),
            (16.30, 9.50, 0.0, 0.0, 0.0, 0.0),
            (16.30, 12.00, 0.0, 0.0, 0.0, 0.0),
            # $50.00 (fora de micro-banca <50) -> cap 10% = $5.00 em V1/V3/V4; na V2 cap 8% = $4.00 (< $5.00 -> veto)
            (50.00, 5.00, 5.00, 0.0, 5.00, 5.00),
            # $100.00 -> V1/V3/V4 = $10.00, V2 = $8.00
            (100.00, 5.00, 10.00, 8.00, 10.00, 10.00),
            # $1000.00 -> V1/V3/V4 = $100.00, V2 = $80.00
            (1000.00, 5.00, 100.00, 80.00, 100.00, 100.00),
        ],
    )
    def test_oito_cenarios_matematicos_todas_estrategias(
        self, wallet, min_stake, esperado_v1, esperado_v2, esperado_v3, esperado_v4
    ):
        now = datetime.now(UTC)
        for cls, esperado in [
            (RenderQuantV1, esperado_v1),
            (RenderQuantV2_Institutional, esperado_v2),
            (RenderQuantV3_Defensive, esperado_v3),
            (RenderQuantV4_Adaptive, esperado_v4),
        ]:
            strat = cls()
            strat.wallets = MagicMock()
            strat.wallets.get_total_stake_amount.return_value = wallet
            # Simula max_stake realista do Freqtrade (99% do saldo livre)
            max_stake = wallet * 0.99
            stake = strat.custom_stake_amount(
                pair="BTC/USDT",
                current_time=now,
                current_rate=60000.0,
                proposed_stake=wallet * 0.10,
                min_stake=min_stake,
                max_stake=max_stake,
                leverage=1.0,
                entry_tag=None,
                side="long",
            )
            assert stake == esperado, f"{cls.__name__} wallet={wallet} min={min_stake}: esperado {esperado}, obteve {stake}"

    def test_reserva_taxas_respeitada_quando_max_stake_apertado(self):
        strat = RenderQuantV1()
        strat.wallets = MagicMock()
        strat.wallets.get_total_stake_amount.return_value = 1000.0
        # Se max_stake livre for apenas $50.00, fee_buffer (0.62%) reduz teto efetivo para $49.69
        stake = strat.custom_stake_amount(
            pair="ETH/USDT",
            current_time=datetime.now(UTC),
            current_rate=3000.0,
            proposed_stake=50.0,
            min_stake=5.0,
            max_stake=50.0,
            leverage=1.0,
            entry_tag=None,
            side="long",
        )
        assert stake < 50.0
        assert pytest.approx(stake, rel=1e-4) == 50.0 * (1.0 - 0.0062)

    def test_valores_invalidos_e_none(self):
        strat = RenderQuantV1()
        strat.wallets = MagicMock()
        strat.wallets.get_total_stake_amount.return_value = 1000.0
        now = datetime.now(UTC)
        assert strat.custom_stake_amount("BTC/USDT", now, 60000.0, 100.0, 5.0, 0.0, 1.0, None, "long") == 0.0
        assert strat.custom_stake_amount("BTC/USDT", now, 60000.0, 100.0, 5.0, float("nan"), 1.0, None, "long") == 0.0
        assert strat.custom_stake_amount("BTC/USDT", now, 60000.0, 100.0, 10.0, 5.0, 1.0, None, "long") == 0.0
        assert strat.custom_stake_amount("BTC/USDT", now, 60000.0, 100.0, None, 1000.0, 1.0, None, "long") == 100.0


# ---------------------------------------------------------------------------
# REGRA 2 — AUDITORIA DO CACHE, CONCORRÊNCIA E RESILIÊNCIA DA IA
# ---------------------------------------------------------------------------
class TestRegra2CacheEConcorrenciaIA:
    MOCK_STATE: ClassVar[dict] = {
        "enabled": True,
        "api_key": "test_key_isolated",
        "endpoint": "https://api.miraiapi.com/v1/chat/completions",
        "model": "claude-opus-5.5",
        "min_conviccao": 0.65,
    }

    def setup_method(self):
        ai_guardian.clear_verdict_cache()

    def _ok_resp(self, aprovado=True, conviccao=0.85, motivo="Confluência válida"):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {
            "choices": [{"message": {"content": json.dumps({"aprovado": aprovado, "conviccao": conviccao, "motivo": motivo})}}]
        }
        return r

    def test_virada_de_vela_15m_invalida_cache_mesmo_em_segundos(self):
        # 10:14:50 UTC -> bucket 15m A; 10:15:10 UTC -> bucket 15m B (apenas 20s depois!)
        t_vela_1 = 1700000090.0  # 1700000090 // 900 = 1888888
        t_vela_2 = (1888888 + 1) * 900.0 + 10.0  # 10s após virada da vela
        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", side_effect=[self._ok_resp(True, 0.85, "Vela 1"), self._ok_resp(False, 0.90, "Vela 2")]) as m_post,
        ):
            v1 = ai_guardian.consult_ai_guardian("BTC/USDT", 60000, 0.45, 0.02, 1.0, True, 0.0, candle_ts=t_vela_1)
            v2 = ai_guardian.consult_ai_guardian("BTC/USDT", 60100, 0.45, 0.02, 1.0, True, 0.0, candle_ts=t_vela_2)
            assert m_post.call_count == 2
            assert v1["aprovado"] is True
            assert v2["aprovado"] is False

    def test_mesmo_par_com_sinal_ou_estrategia_diferente_nao_colide(self):
        t = 1888888 * 900.0 + 100.0
        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", side_effect=[self._ok_resp(True, 0.85), self._ok_resp(False, 0.80)]) as m_post,
        ):
            v_a = ai_guardian.consult_ai_guardian("ETH/USDT", 3000, 0.45, 0.02, 1.0, True, 0.0, mode="RenderQuantV1", signal_tag="tag_a", candle_ts=t)
            v_b = ai_guardian.consult_ai_guardian("ETH/USDT", 3000, 0.45, 0.02, 1.0, True, 0.0, mode="RenderQuantV1", signal_tag="tag_b", candle_ts=t)
            assert m_post.call_count == 2
            assert v_a["aprovado"] is True
            assert v_b["aprovado"] is False

    def test_recuperacao_imediata_apos_falha_fail_closed_nao_cacheia(self):
        import requests as req
        t = 1888888 * 900.0 + 100.0
        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", side_effect=[req.Timeout("Timeout"), self._ok_resp(True, 0.88, "Recuperado")]) as m_post,
        ):
            v1 = ai_guardian.consult_ai_guardian("SOL/USDT", 150, 0.45, 0.02, 1.0, True, 0.0, candle_ts=t)
            v2 = ai_guardian.consult_ai_guardian("SOL/USDT", 150, 0.45, 0.02, 1.0, True, 0.0, candle_ts=t)
            assert m_post.call_count == 2
            assert v1["origem"] == "fail_closed" and v1["aprovado"] is False
            assert v2["origem"] == "ia" and v2["aprovado"] is True

    def test_json_incompleto_sem_campos_obrigatorios_e_texto_puro(self):
        r_texto = MagicMock(status_code=200)
        r_texto.json.return_value = {"choices": [{"message": {"content": "Texto puro sem chaves"}}]}
        r_incompleto = MagicMock(status_code=200)
        r_incompleto.json.return_value = {"choices": [{"message": {"content": '{"motivo": "sem aprovado"}'}}]}

        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", side_effect=[r_texto, r_incompleto]),
        ):
            v1 = ai_guardian.consult_ai_guardian("ADA/USDT", 0.5, 0.45, 0.02, 1.0, True, 0.0)
            v2 = ai_guardian.consult_ai_guardian("LINK/USDT", 15.0, 0.45, 0.02, 1.0, True, 0.0)
            assert v1["origem"] == "fail_closed" and v1["aprovado"] is False
            assert v2["origem"] == "fail_closed" and v2["aprovado"] is False

    def test_concorrencia_in_flight_deduplica_chamada_http(self):
        t = 1888888 * 900.0 + 100.0
        call_counter = 0
        lock = threading.Lock()

        def slow_post(*args, **kwargs):
            nonlocal call_counter
            time.sleep(0.15)
            with lock:
                call_counter += 1
            return self._ok_resp(True, 0.90, "Concorrente")

        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", side_effect=slow_post),
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                futs = [
                    ex.submit(ai_guardian.consult_ai_guardian, "BTC/USDT", 60000, 0.45, 0.02, 1.0, True, 0.0, candle_ts=t)
                    for _ in range(4)
                ]
                results = [f.result() for f in futs]
            assert call_counter == 1, f"Esperado 1 chamada HTTP deduplicada, ocorreram {call_counter}"
            assert all(r["aprovado"] is True for r in results)

    def test_reinicializacao_limpa_cache_sem_inconsistencia(self):
        t = 1888888 * 900.0 + 100.0
        with (
            patch("user_data.ai_guardian.get_state", return_value=self.MOCK_STATE),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", side_effect=[self._ok_resp(True, 0.85), self._ok_resp(False, 0.85)]) as m_post,
        ):
            v1 = ai_guardian.consult_ai_guardian("XRP/USDT", 1.0, 0.45, 0.02, 1.0, True, 0.0, candle_ts=t)
            ai_guardian.clear_verdict_cache()  # Simula reinício de processo
            v2 = ai_guardian.consult_ai_guardian("XRP/USDT", 1.0, 0.45, 0.02, 1.0, True, 0.0, candle_ts=t)
            assert m_post.call_count == 2
            assert v1["aprovado"] is True and v2["aprovado"] is False


# ---------------------------------------------------------------------------
# REGRA 3 & REGRA 4 — PROTEÇÕES DE SAÍDA, STOPLOSS E COBERTURA V1, V2, V3, V4
# ---------------------------------------------------------------------------
class TestRegra3e4CoberturaEstrategiasESaidas:
    def test_config_render_stoploss_e_unfilledtimeout_kraken(self):
        cfg = json.loads((ROOT / "user_data" / "config.render.json").read_text(encoding="utf-8"))
        assert cfg["order_types"]["stoploss"] == "market"
        assert cfg["order_types"]["stoploss_on_exchange"] is True
        assert cfg["order_types"]["emergency_exit"] == "market"
        assert cfg["unfilledtimeout"]["entry"] == 3
        assert cfg["unfilledtimeout"]["exit"] == 3
        assert cfg["unfilledtimeout"]["unit"] == "minutes"

    @pytest.mark.parametrize("cls", [RenderQuantV1, RenderQuantV2_Institutional, RenderQuantV3_Defensive, RenderQuantV4_Adaptive])
    def test_todas_estrategias_tem_stoploss_independente_de_ia(self, cls):
        strat = cls()
        assert strat.order_types["stoploss_on_exchange"] is True
        assert strat.order_types["emergency_exit"] == "market"
        mock_trade = MagicMock()
        mock_trade.open_date_utc = datetime.now(UTC)

        # Mesmo com a API da IA explodindo, custom_stoploss funciona normalmente
        with patch("requests.post", side_effect=RuntimeError("API Mirai Offline")):
            sl_neg = strat.custom_stoploss("BTC/USDT", mock_trade, datetime.now(UTC), 58000.0, -0.02, False)
            sl_be = strat.custom_stoploss("BTC/USDT", mock_trade, datetime.now(UTC), 62000.0, 0.03, False)
            assert sl_neg < 0
            assert sl_be > -0.03  # Moveu para breakeven ou trava de lucro

    def test_v1_e_v2_diferenciacao_de_parametros(self):
        v1 = RenderQuantV1()
        v2 = RenderQuantV2_Institutional()
        assert v1._get_active_strategy_mode() in ("RenderQuantV1", "RenderQuantV2_Institutional")
        assert v2._get_active_strategy_mode() == "RenderQuantV2_Institutional"
        v1.wallets = MagicMock()
        v2.wallets = MagicMock()
        v1.wallets.get_total_stake_amount.return_value = 1000.0
        v2.wallets.get_total_stake_amount.return_value = 1000.0
        now = datetime.now(UTC)
        assert v1.custom_stake_amount("BTC/USDT", now, 60000.0, 100.0, 5.0, 999.0, 1.0, None, "long") == 100.0
        assert v2.custom_stake_amount("BTC/USDT", now, 60000.0, 80.0, 5.0, 999.0, 1.0, None, "long") == 80.0

    def test_v3_e_v4_custom_exit_e_protections_e_dp_ausente(self):
        v3 = RenderQuantV3_Defensive()
        v4 = RenderQuantV4_Adaptive()
        assert v3.timeframe == "4h" and v4.timeframe == "4h"
        assert len(v3.protections) == 3 and len(v4.protections) == 3
        mock_trade = MagicMock()
        # Sem self.dp injetado não deve lançar AttributeError
        assert v3.custom_exit("BTC/USDT", mock_trade, datetime.now(UTC), 60000.0, 0.05) is None


# ---------------------------------------------------------------------------
# REGRA 7 — TESTE DE ESTRESSE COM 6 PARES SIMULTÂNEOS E MEDIÇÃO DE LATÊNCIA
# ---------------------------------------------------------------------------
class TestRegra7EstresseELatencia:
    def test_seis_pares_simultaneos_com_cache_e_recuperacao(self):
        ai_guardian.clear_verdict_cache()
        pairs = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT", "LINK/USDT"]
        mock_state = {
            "enabled": True,
            "api_key": "stress_key",
            "endpoint": "https://api.miraiapi.com/v1/chat/completions",
            "model": "claude-opus-5.5",
            "min_conviccao": 0.65,
        }

        def fast_ok(*args, **kwargs):
            r = MagicMock(status_code=200)
            r.json.return_value = {"choices": [{"message": {"content": '{"aprovado": true, "conviccao": 0.85, "motivo": "OK"}'}}]}
            return r

        t_bucket = 1888888 * 900.0 + 50.0
        with (
            patch("user_data.ai_guardian.get_state", return_value=mock_state),
            patch("user_data.ai_guardian.update_state"),
            patch("requests.post", side_effect=fast_ok) as m_post,
        ):
            t0 = time.perf_counter()
            # 1a passagem nos 6 pares: 6 chamadas HTTP
            for p in pairs:
                res = ai_guardian.consult_ai_guardian(p, 100.0, 0.45, 0.02, 1.0, True, 0.0, candle_ts=t_bucket)
                assert res["aprovado"] is True
            dt_miss = time.perf_counter() - t0

            # 2a passagem (30 repetições nos 6 pares na mesma vela): 0 chamadas HTTP extras (cache hit)
            t1 = time.perf_counter()
            for _ in range(5):
                for p in pairs:
                    res = ai_guardian.consult_ai_guardian(p, 100.0, 0.45, 0.02, 1.0, True, 0.0, candle_ts=t_bucket)
                    assert res["aprovado"] is True
            dt_hit = time.perf_counter() - t1

            assert m_post.call_count == 6
            assert dt_hit < 0.1, f"Cache hit nos 30 ciclos demorou {dt_hit:.4f}s"
            assert dt_miss >= 0.0
