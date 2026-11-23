"""
Testes unitários rigorosos para user_data/shadow_tracker.py.
Valida criação de tabelas, registro de sinais sombra, encerramento por TP/SL,
cálculo de estatísticas e formatação de texto para o Telegram.
"""
from unittest.mock import MagicMock, patch
import pytest

from user_data.shadow_tracker import (
    _kraken_symbol_map,
    ensure_shadow_tables,
    format_shadow_stats_text,
    get_shadow_stats,
    record_shadow_signal,
    update_open_shadow_trades,
)


def test_kraken_symbol_map():
    assert _kraken_symbol_map("BTC/USDT") == "XBTUSDT"
    assert _kraken_symbol_map("ETH/USDT") == "ETHUSDT"
    assert _kraken_symbol_map("SOL/USDT") == "SOLUSDT"


@patch("user_data.shadow_tracker._get_connection")
def test_ensure_shadow_tables(mock_get_conn):
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_get_conn.return_value = mock_conn

    assert ensure_shadow_tables() is True
    assert mock_cur.execute.called


@patch("user_data.shadow_tracker._get_connection")
def test_record_shadow_signal(mock_get_conn):
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_get_conn.return_value = mock_conn

    # 1. Sem duplicação
    mock_cur.fetchone.side_effect = [None, (42,)]

    tid = record_shadow_signal(
        pair="BTC/USDT",
        strategy="RenderQuantV4_Adaptive",
        entry_rate=65000.0,
        entry_tag="v4_oversold_bounce",
        ai_approved=True,
        ai_conviccao=0.88,
        ai_motivo="Forte confluência e KER ascendente",
    )
    assert tid == 42
    assert mock_cur.execute.call_count == 2


@patch("user_data.shadow_tracker._get_connection")
def test_update_open_shadow_trades_tp_and_sl(mock_get_conn):
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_get_conn.return_value = mock_conn

    # Trades abertos:
    # 1. BTC/USDT: entry 100, target 105, stop 95 (vai atingir TP com taxa 106)
    # 2. ETH/USDT: entry 100, target 105, stop 95 (vai atingir SL com taxa 94)
    # 3. SOL/USDT: entry 100, target 105, stop 95 (continua aberto com taxa 102)
    open_trades = [
        (1, "BTC/USDT", 100.0, 105.0, 95.0, 100.0, 100.0, "2026-11-22"),
        (2, "ETH/USDT", 100.0, 105.0, 95.0, 100.0, 100.0, "2026-11-22"),
        (3, "SOL/USDT", 100.0, 105.0, 95.0, 100.0, 100.0, "2026-11-22"),
    ]
    mock_cur.fetchall.return_value = open_trades

    rates = {
        "BTC/USDT": 106.0,
        "ETH/USDT": 94.0,
        "SOL/USDT": 102.0,
    }

    closed = update_open_shadow_trades(lambda p: rates.get(p))
    assert closed == 2  # 1 TP + 1 SL fechados


@patch("user_data.shadow_tracker._get_connection")
def test_get_shadow_stats(mock_get_conn):
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_get_conn.return_value = mock_conn

    # Mock das queries de contagem e métricas
    mock_cur.fetchone.side_effect = [
        (10,),     # total
        (2,),      # open
        (7,),      # profit
        (1,),      # stop
        (0.245,),  # cum_profit_pct (24.5%)
        (4,),      # ai_vetoes_total
        (3,),      # ai_vetoes_saved_loss
    ]
    mock_cur.fetchall.return_value = []

    st = get_shadow_stats()
    assert st["total_signals"] == 10
    assert st["open_signals"] == 2
    assert st["closed_profit"] == 7
    assert st["closed_stop"] == 1
    assert st["win_rate"] == 87.5
    assert st["cum_profit_pct"] == 24.5
    assert st["ai_vetoes_total"] == 4
    assert st["ai_vetoes_saved_loss"] == 3
    assert st["ai_veto_success_rate"] == 75.0


@patch("user_data.shadow_tracker.get_shadow_stats")
def test_format_shadow_stats_text(mock_get_stats):
    mock_get_stats.return_value = {
        "total_signals": 10,
        "open_signals": 2,
        "closed_profit": 7,
        "closed_stop": 1,
        "win_rate": 87.5,
        "cum_profit_pct": 24.5,
        "ai_vetoes_total": 4,
        "ai_vetoes_saved_loss": 3,
        "ai_veto_success_rate": 75.0,
        "last_trades": [],
    }

    text = format_shadow_stats_text()
    assert "Telemetria de Shadow Trading" in text
    assert "87.5%" in text
    assert "Claude Opus 5.5" in text
    assert "+24.50%" in text
