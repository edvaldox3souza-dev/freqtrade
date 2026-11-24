import os
from unittest.mock import patch

import start_bot


def test_available_strategies_catalog():
    strat_dict = dict(start_bot.AVAILABLE_STRATEGIES)
    assert "RenderQuantV1" in strat_dict
    assert "RenderQuantV2_Institutional" in strat_dict
    assert "RenderQuantV3_Defensive" in strat_dict
    assert "RenderQuantV4_Adaptive" in strat_dict


def test_get_and_update_strategy_state(tmp_path):
    mock_file = tmp_path / "strategy_state.json"
    with patch("start_bot.STRATEGY_STATE_PATH", mock_file):
        # Default state
        state = start_bot.get_strategy_state()
        assert state["active_strategy"] in dict(start_bot.AVAILABLE_STRATEGIES)

        # Update state
        updated = start_bot.update_strategy_state("RenderQuantV4_Adaptive")
        assert updated["active_strategy"] == "RenderQuantV4_Adaptive"
        assert "updated_at" in updated

        # Verify load after update
        loaded = start_bot.get_strategy_state()
        assert loaded["active_strategy"] == "RenderQuantV4_Adaptive"


def test_web_pt_br_script_contains_strategy_modal():
    script = start_bot.build_web_pt_br_script()
    assert "mountStratModal" in script
    assert "unk-strat-btn" in script
    assert "unk-strat-ov" in script
    assert "/api/v1/unkiorra/strategy" in script
    assert "RenderQuantV3_Defensive" in script
    assert "RenderQuantV4_Adaptive" in script


def test_inject_web_pt_br_html():
    raw_html = b"<html><head><title>FreqUI</title></head><body><h1>FreqUI Content</h1></body></html>"
    injected = start_bot.inject_web_pt_br_html(raw_html)
    injected_str = injected.decode("utf-8")
    assert "<title>Unkiorra Trade Bot — Painel Web (PT-BR)</title>" in injected_str
    assert "unk-strat-btn" in injected_str
    assert "mountStratModal" in injected_str


def test_strategy_precedence_over_env_var(tmp_path):
    import json
    src_cfg = tmp_path / "config.src.json"
    dst_cfg = tmp_path / "config.runtime.json"
    state_file = tmp_path / "strategy_state.json"

    src_cfg.write_text(json.dumps({"strategy": "RenderQuantV3_Defensive", "dry_run": True}), encoding="utf-8")

    with patch.dict(os.environ, {"FREQTRADE_STRATEGY": "RenderQuantV3_Defensive"}), \
         patch("start_bot.STRATEGY_STATE_PATH", state_file), \
         patch("start_bot.RUNTIME_CONFIG_PATH", dst_cfg):
        
        # Usuário seleciona V4 no bot
        start_bot.update_strategy_state("RenderQuantV4_Adaptive")
        
        # Executa ensure_runtime_config (como no boot)
        start_bot.ensure_runtime_config(src_path=src_cfg, dst_path=dst_cfg)
        
        # Verifica se o arquivo em disco recebeu V4 e NÃO a env var V3
        cfg = json.loads(dst_cfg.read_text(encoding="utf-8"))
        assert cfg["strategy"] == "RenderQuantV4_Adaptive"


def test_boot_strategy_resolution_with_user_state(tmp_path):
    state_file = tmp_path / "strategy_state.json"
    with patch.dict(os.environ, {"FREQTRADE_STRATEGY": "RenderQuantV3_Defensive"}), \
         patch("start_bot.STRATEGY_STATE_PATH", state_file):
        
        # Usuário selecionou V2 Institucional
        start_bot.update_strategy_state("RenderQuantV2_Institutional")
        
        # Simula o bloco de resolução do boot
        st_val = start_bot.get_strategy_state().get("active_strategy", "").strip()
        boot_strategy = st_val or os.environ.get("FREQTRADE_STRATEGY", "").strip() or "RenderQuantV3_Defensive"
        
        assert boot_strategy == "RenderQuantV2_Institutional"


def test_force_strategy_override_admin(tmp_path):
    state_file = tmp_path / "strategy_state.json"
    with patch.dict(os.environ, {
        "FREQTRADE_STRATEGY": "RenderQuantV3_Defensive",
        "FORCE_STRATEGY_OVERRIDE": "RenderQuantV1"
    }), patch("start_bot.STRATEGY_STATE_PATH", state_file):
        
        start_bot.update_strategy_state("RenderQuantV4_Adaptive")
        
        force_override = os.environ.get("FORCE_STRATEGY_OVERRIDE", "").strip()
        if force_override:
            boot_strategy = force_override
        else:
            boot_strategy = start_bot.get_strategy_state().get("active_strategy", "").strip()
        
        assert boot_strategy == "RenderQuantV1"


def test_db_get_handles_both_dict_and_str():
    from unittest.mock import MagicMock

    from user_data.state_store import db_get

    # Caso 1: PostgreSQL psycopg2 retorna coluna JSONB como dict Python
    mock_cursor_dict = MagicMock()
    mock_cursor_dict.fetchone.return_value = ({"active_strategy": "RenderQuantV4_Adaptive"},)
    mock_cursor_dict.__enter__.return_value = mock_cursor_dict

    mock_conn_dict = MagicMock()
    mock_conn_dict.cursor.return_value = mock_cursor_dict
    mock_conn_dict.__enter__.return_value = mock_conn_dict

    with patch("user_data.state_store._connect", return_value=mock_conn_dict):
        res = db_get("strategy_state")
        assert res == {"active_strategy": "RenderQuantV4_Adaptive"}

    # Caso 2: Se retornar string json
    mock_cursor_str = MagicMock()
    mock_cursor_str.fetchone.return_value = ('{"active_strategy": "RenderQuantV2_Institutional"}',)
    mock_cursor_str.__enter__.return_value = mock_cursor_str

    mock_conn_str = MagicMock()
    mock_conn_str.cursor.return_value = mock_cursor_str
    mock_conn_str.__enter__.return_value = mock_conn_str

    with patch("user_data.state_store._connect", return_value=mock_conn_str):
        res = db_get("strategy_state")
        assert res == {"active_strategy": "RenderQuantV2_Institutional"}


def test_strategy_timeframe_synchronization(tmp_path):
    import json
    src_cfg = tmp_path / "config.src.json"
    dst_cfg = tmp_path / "config.runtime.json"
    state_file = tmp_path / "strategy_state.json"

    src_cfg.write_text(json.dumps({"strategy": "RenderQuantV3_Defensive", "timeframe": "4h"}), encoding="utf-8")

    # Caso V1: deve sincronizar timeframe para 15m
    with patch("start_bot.STRATEGY_STATE_PATH", state_file), \
         patch("start_bot.RUNTIME_CONFIG_PATH", dst_cfg):
        start_bot.update_strategy_state("RenderQuantV1")
        start_bot.ensure_runtime_config(src_path=src_cfg, dst_path=dst_cfg)
        cfg = json.loads(dst_cfg.read_text(encoding="utf-8"))
        assert cfg["strategy"] == "RenderQuantV1"
        assert cfg["timeframe"] == "15m"

        msg = start_bot.get_welcome_message()
        assert "RenderQuantV1` `15m`" in msg

    # Caso V3: deve manter/sincronizar timeframe para 4h
    with patch("start_bot.STRATEGY_STATE_PATH", state_file), \
         patch("start_bot.RUNTIME_CONFIG_PATH", dst_cfg):
        start_bot.update_strategy_state("RenderQuantV3_Defensive")
        start_bot.ensure_runtime_config(src_path=src_cfg, dst_path=dst_cfg)
        cfg = json.loads(dst_cfg.read_text(encoding="utf-8"))
        assert cfg["strategy"] == "RenderQuantV3_Defensive"
        assert cfg["timeframe"] == "4h"

        msg = start_bot.get_welcome_message()
        assert "RenderQuantV3_Defensive` `4h`" in msg



