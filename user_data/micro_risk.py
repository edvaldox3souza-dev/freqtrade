"""Regras compartilhadas de risco e sizing calibradas para micro-bancas (< $50 USDT) e contas institucionais."""
import logging

MICRO_WALLET_THRESHOLD = 50.0
MICRO_SLOT_MAX_RATIO = 0.60
MICRO_MAX_STOP_LOSS_PCT = 0.05
MICRO_PORTFOLIO_HEAT_LIMIT = 0.12


def resolve_stake_with_micro_floor(
    *,
    pair: str,
    prefix: str,
    total_wallet: float,
    cap: float,
    min_stake: float | None,
    effective_max_stake: float,
    stressed_dist_pct: float,
    strat_logger: logging.Logger,
) -> float:
    """Adequa ordens abaixo do mínimo da corretora em micro-bancas (< $50) sem causar InsufficientFunds.

    Bancas >= $50 mantêm veto caso `cap < min_stake` (política institucional estrita).
    Micro-bancas (< $50) liberam até `min_stake` sempre que couber no saldo livre pós-taxas,
    respeitar o teto de 60% por posição e perda máxima estimada de 5% no stoploss.
    """
    if min_stake is None or cap >= float(min_stake):
        return cap

    f_min_stake = float(min_stake)
    max_micro_slot = total_wallet * MICRO_SLOT_MAX_RATIO
    est_stop_loss_usd = f_min_stake * stressed_dist_pct
    max_stop_loss_usd = total_wallet * MICRO_MAX_STOP_LOSS_PCT

    if (
        total_wallet < MICRO_WALLET_THRESHOLD
        and f_min_stake <= effective_max_stake
        and f_min_stake <= max_micro_slot
        and est_stop_loss_usd <= max_stop_loss_usd
    ):
        strat_logger.info(
            "%s [MICRO-STAKE RISK-BOUNDED]: %s ajustado para lote mínimo Kraken $%.2f "
            "(%.1f%% da banca $%.2f | perda est. stop=$%.2f = %.2f%% <= 5.00%%).",
            prefix,
            pair,
            f_min_stake,
            (f_min_stake / max(total_wallet, 1e-9)) * 100.0,
            total_wallet,
            est_stop_loss_usd,
            (est_stop_loss_usd / max(total_wallet, 1e-9)) * 100.0,
        )
        return f_min_stake

    strat_logger.warning(
        "%s [RISK VETO]: Ordem em %s recusada – min_stake ($%.2f) excede o limite aprovado "
        "(cap=$%.2f | livre_max=$%.2f | slot_micro=$%.2f | perda_stop=$%.2f vs máx=$%.2f | banca=$%.2f).",
        prefix,
        pair,
        f_min_stake,
        cap,
        effective_max_stake,
        max_micro_slot,
        est_stop_loss_usd,
        max_stop_loss_usd,
        total_wallet,
    )
    return 0.0


def effective_portfolio_heat_limit(total_balance: float, institutional_limit: float = 0.03) -> float:
    """Retorna 12% de heat para micro-bancas (< $50) permitindo até 3 posições; >= $50 mantém limite institucional."""
    if total_balance < MICRO_WALLET_THRESHOLD:
        return MICRO_PORTFOLIO_HEAT_LIMIT
    return institutional_limit


def effective_cluster_limit(total_balance: float, base_limit: int = 1) -> int:
    """Em micro-bancas (< $50), permite até 2 pares no mesmo cluster para evitar travamento excessivo."""
    if total_balance < MICRO_WALLET_THRESHOLD:
        return max(int(base_limit), 2)
    return int(base_limit)


def is_btc_macro_vetoed(
    btc_close: float,
    btc_sma50: float,
    btc_ema9: float,
    btc_ema21: float,
    btc_macd_hist: float,
) -> tuple[bool, str]:
    """Proteção anti-crash real sem bloquear altcoins por ruído leve abaixo da SMA50.

    Libera sempre que houver retomada de momentum acima de -2.5% da SMA50 ou quando o BTC
    estiver próximo da média (>= -1.0%) sem tendência de queda dupla ativa.
    """
    if btc_close <= 0 or btc_sma50 <= 0:
        return False, ""

    btc_early_recovery = (
        btc_ema9 > 0
        and btc_ema21 > 0
        and btc_ema9 > btc_ema21
        and btc_macd_hist > 0
        and btc_close >= btc_sma50 * 0.975
    )
    if btc_early_recovery:
        return False, ""

    btc_severe_dump = btc_close < btc_sma50 * 0.970
    btc_active_downtrend = (
        btc_close < btc_sma50 * 0.990
        and btc_ema9 > 0
        and btc_ema21 > 0
        and btc_ema9 < btc_ema21
        and btc_macd_hist < 0
    )
    if btc_severe_dump or btc_active_downtrend:
        return (
            True,
            f"Regime Macro Anti-Crash: BTC (${btc_close:.0f}) em queda abaixo da SMA50 (${btc_sma50:.0f})",
        )
    return False, ""
