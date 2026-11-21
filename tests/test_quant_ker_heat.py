import pytest
import numpy as np
import pandas as pd


def compute_ker(close: pd.Series, window: int = 14) -> pd.Series:
    """KER = |close_t - close_{t-window}| / sum(|close_i - close_{i-1}|)"""
    direction = (close - close.shift(window)).abs()
    volatility = close.diff().abs().rolling(window, min_periods=window).sum()
    safe_vol = volatility.replace(0, np.nan)
    ker = direction / safe_vol
    return ker.clip(lower=0.0, upper=1.0).fillna(0.0)


def test_ker_monotonic_trend():
    # Série perfeitamente direcional: 10, 20, 30, ..., 200
    prices = pd.Series([10.0 + i * 5.0 for i in range(30)])
    ker = compute_ker(prices, window=14)
    # Em uma linha reta perfeita, KER deve ser exatamente 1.0
    assert pytest.approx(ker.iloc[-1], 0.01) == 1.0


def test_ker_chop_noise():
    # Série em consolidação ruidosa: oscilando entre 100 e 101
    prices = pd.Series([100.0 if i % 2 == 0 else 101.0 for i in range(30)])
    ker = compute_ker(prices, window=14)
    # Em ruído puro, o deslocamento líquido é <= 1.0 mas a soma dos passos é 14.0 -> KER ~ 0.07 ou 0.0
    assert ker.iloc[-1] < 0.15


def test_portfolio_heat_gate():
    total_balance = 1000.0
    heat_limit = 0.03 # 3% = $30 max total risk

    # 2 trades abertos com risco de $10 cada um
    open_risks = [10.0, 10.0] # Total atual: $20 (2% heat)

    # Novo trade com risco de $8
    incoming_risk_valid = 8.0 # Total: $28 (2.8% <= 3%)
    assert (sum(open_risks) + incoming_risk_valid) / total_balance <= heat_limit

    # Novo trade com risco de $15
    incoming_risk_exceeds = 15.0 # Total: $35 (3.5% > 3%)
    assert (sum(open_risks) + incoming_risk_exceeds) / total_balance > heat_limit
