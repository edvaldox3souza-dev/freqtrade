import pytest
import json
import os
import pathlib


class DummyTrade:
    def __init__(self, enter_tag="V1:equilibrada_pullback"):
        self.enter_tag = enter_tag
        self.open_rate = 100.0


def test_custom_stoploss_breakeven_ladder():
    # Simula a lógica de custom_stoploss
    round_trip_cost = 0.0062

    def calc_stop(current_profit, atr_pct=0.015):
        dynamic_stop = -max(0.015, min(2.0 * atr_pct, 0.040))
        if current_profit >= 0.012 and current_profit < 0.025:
            return max(dynamic_stop, 0.0025)
        if current_profit >= 0.025 and current_profit < 0.040:
            return max(dynamic_stop, 0.0120)
        if current_profit >= 0.040:
            trailing_dist = max(atr_pct * 1.5, 0.015)
            desired_stop = current_profit - trailing_dist
            return max(dynamic_stop, desired_stop, 0.0250)
        return dynamic_stop

    # 1. Trade recente no início (+0.5%): mantém stop loss normal negativo
    s1 = calc_stop(0.005)
    assert s1 < 0.0, f"Expected negative stoploss, got {s1}"

    # 2. Degrau 1 (+1.5%): trava breakeven líquido positivo (+0.25%)
    s2 = calc_stop(0.015)
    assert s2 == 0.0025, f"Expected +0.0025, got {s2}"

    # 3. Degrau 2 (+3.0%): trava lucro líquido de +1.2%
    s3 = calc_stop(0.030)
    assert s3 == 0.0120, f"Expected +0.0120, got {s3}"

    # 4. Degrau 3 (+5.0%): trailing dinâmico garantindo pelo menos +2.5%
    s4 = calc_stop(0.050)
    assert s4 >= 0.0250, f"Expected >= +0.0250, got {s4}"
    print("Test custom_stoploss ladder PASSED!")


def test_why_state_serialization(tmp_path):
    p = tmp_path / "why_state.json"
    payload = {
        "ts": "23:45:00 UTC",
        "pair": "SOL/USD",
        "decision": "REJECT",
        "reason": "Kaufman Efficiency Ratio (KER=0.21 < 0.35) indicando mercado lateral/ruído",
        "heat": 0.018,
    }
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    loaded = json.loads(p.read_text(encoding="utf-8"))
    assert loaded["decision"] == "REJECT"
    assert "KER=0.21" in loaded["reason"]
    print("Test why_state serialization PASSED!")


if __name__ == "__main__":
    test_custom_stoploss_breakeven_ladder()
    test_why_state_serialization(pathlib.Path("."))
    print("ALL TRIAD TESTS COMPLETED SUCCESSFULLY!")
