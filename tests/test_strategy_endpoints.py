import os
import pathlib
import pytest
from unittest.mock import patch, MagicMock

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
