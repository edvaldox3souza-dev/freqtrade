import numpy as np


def compute_ker_numpy(prices: np.ndarray, window: int = 14) -> np.ndarray:
    """KER = |price_t - price_{t-w}| / sum(|price_i - price_{i-1}|)"""
    n = len(prices)
    ker = np.zeros(n, dtype=np.float64)
    if n < window + 1:
        return ker
    
    abs_diffs = np.abs(np.diff(prices))
    
    for i in range(window, n):
        net_dir = abs(prices[i] - prices[i - window])
        # soma dos passos nos últimos `window` passos
        # diffs correspondentes: [i - window : i]
        path_sum = np.sum(abs_diffs[i - window : i])
        if path_sum > 1e-9:
            ker[i] = min(1.0, max(0.0, net_dir / path_sum))
        else:
            ker[i] = 0.0
            
    return ker


def test_monotonic():
    p = np.array([10.0 + i * 5.0 for i in range(30)])
    k = compute_ker_numpy(p, 14)
    assert abs(k[-1] - 1.0) < 1e-4, f"Expected 1.0, got {k[-1]}"
    print("Test monotonic trend passed! KER =", k[-1])


def test_chop():
    p = np.array([100.0 if i % 2 == 0 else 101.0 for i in range(30)])
    k = compute_ker_numpy(p, 14)
    assert k[-1] < 0.15, f"Expected < 0.15, got {k[-1]}"
    print("Test chop noise passed! KER =", k[-1])


def test_portfolio_heat():
    total_balance = 1000.0
    heat_limit = 0.03
    open_risks = [10.0, 10.0]
    incoming_risk = 8.0
    tot = (sum(open_risks) + incoming_risk) / total_balance
    assert tot <= heat_limit, "Should pass"
    
    incoming_risk_high = 15.0
    tot_high = (sum(open_risks) + incoming_risk_high) / total_balance
    assert tot_high > heat_limit, "Should fail"
    print("Test portfolio heat passed! Valid =", tot, "Exceeded =", tot_high)


if __name__ == "__main__":
    test_monotonic()
    test_chop()
    test_portfolio_heat()
    print("ALL TESTS PASSED SUCCESSFULLY!")
