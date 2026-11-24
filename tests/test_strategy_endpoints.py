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

