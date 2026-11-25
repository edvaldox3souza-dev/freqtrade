"""Testes unitários e de regressão para a calibração real da RenderQuantV1/V2."""

import asyncio
import json
import pathlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd

_MOCK_MODS = [
    "talib",
    "talib.abstract",
    "freqtrade",
    "freqtrade.persistence",
    "freqtrade.strategy",
]
for mod in _MOCK_MODS:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()


class MockIStrategy:
    pass


sys.modules["freqtrade.strategy"].IStrategy = MockIStrategy

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import start_bot  # noqa: E402
from user_data.strategies.RenderQuantV1 import RenderQuantV1  # noqa: E402
from user_data.strategies.RenderQuantV2_Institutional import (  # noqa: E402
    RenderQuantV2_Institutional,
)


def _base_df(rows: int = 3) -> pd.DataFrame:
    return pd.DataFrame({
        "open": [100.0] * rows,
        "high": [101.0] * rows,
        "low": [99.5] * rows,
        "close": [100.5] * rows,
        "volume": [1000.0] * rows,
        "sma50": [98.0] * rows,
        "sma150": [96.0] * rows,
        "sma200": [95.0] * rows,
        "sma200_slope": [1] * rows,
        "ema9": [100.2] * rows,
        "ema21": [99.8] * rows,
        "rsi": [50.0] * rows,
        "adx": [22.0] * rows,
        "natr": [0.80] * rows,
        "atr_pct": [0.008] * rows,
        "bb_lower": [98.0] * rows,
        "bb_middle": [100.0] * rows,
        "bb_upper": [102.0] * rows,
        "bb_pos": [0.50] * rows,
        "macd": [0.5] * rows,
        "macd_signal": [0.2] * rows,
        "macd_hist": ([0.1, 0.2, 0.3][:rows] + [0.3] * max(0, rows - 3)),
        "pivot_p": [99.5] * rows,
        "pivot_r1": [104.0] * rows,
        "pivot_s1": [97.0] * rows,
        "ker": [0.40] * rows,
        "is_whale_vol": [0] * rows,
        "vol_modified_z": [0.5] * rows,
        "market_regime_bull": [1] * rows,
        "volume_usd_24h": [500_000.0] * rows,
        "stretch_atr": [0.4] * rows,
    })


def test_v1_branches_generate_entries_and_tags():
    strat = RenderQuantV1()
    strat.dp = MagicMock()
    strat.dp.runmode = "backtest"

    # Ramo 1: Retomada de Momentum em Tendência (mesmo com RSI > 48!)
    df_mom = _base_df(3)
    df_mom.loc[2, "low"] = 100.7  # isola ramo trend_mom fora da EMA21
    df_mom.loc[2, "rsi"] = 58.0
    out_mom = strat.populate_entry_trend(df_mom, {"pair": "BTC/USDT"})
    assert out_mom.loc[2, "enter_long"] == 1
    assert out_mom.loc[2, "enter_tag"] == "V1:trend_mom"

    # Ramo 2: Pullback na EMA21 em tendência saudável
    df_pull = _base_df(3)
    df_pull["macd_hist"] = -0.2
    df_pull.loc[1, "rsi"] = 48.0
    df_pull.loc[2, "rsi"] = 51.0
    df_pull.loc[2, "low"] = 99.6
    out_pull = strat.populate_entry_trend(df_pull, {"pair": "SOL/USDT"})
    assert out_pull.loc[2, "enter_long"] == 1
    assert out_pull.loc[2, "enter_tag"] == "V1:equilibrada_pullback"

    # Ramo 3: Capitulação com reversão compradora verde
    df_rev = _base_df(3)
    df_rev["close"] = [92.0, 90.0, 90.8]
    df_rev["open"] = [93.0, 91.0, 89.8]
    df_rev["ema9"] = 89.0
    df_rev["bb_pos"] = [0.20, 0.01, 0.03]
    df_rev["rsi"] = [35.0, 26.0, 28.5]

    # Vela vermelha em sobrevenda NÃO entra (proteção contra faca caindo)
    df_red = df_rev.copy()
    df_red.loc[2, "open"] = 92.0
    df_red.loc[2, "close"] = 90.8

    out_rev = strat.populate_entry_trend(df_rev.copy(), {"pair": "ADA/USDT"})
    assert out_rev.loc[2, "enter_long"] == 1
    assert out_rev.loc[2, "enter_tag"] == "V1:oversold_reclaim"

    out_red = strat.populate_entry_trend(df_red, {"pair": "ADA/USDT"})
    assert out_red.get("enter_long", pd.Series([0] * 3)).iloc[2] != 1

    # Ramo 4: Retomada Antecipada por Cruzamento EMA9 > EMA21 (V1:early_cross)
    df_cross = _base_df(3)
    df_cross["sma50"] = 94.0  # desativa pullback
    df_cross["sma200"] = 105.0  # abaixo da sma200 -> desativa trend_mom
    df_cross["ema9"] = [99.0, 99.5, 100.3]
    df_cross["ema21"] = [99.8, 99.8, 99.9]
    df_cross["macd_hist"] = [-0.1, -0.02, 0.08]
    df_cross["rsi"] = [42.0, 44.0, 49.0]
    out_cross = strat.populate_entry_trend(df_cross, {"pair": "ETH/USDT"})
    assert out_cross.loc[2, "enter_long"] == 1
    assert out_cross.loc[2, "enter_tag"] == "V1:early_cross"


def test_v2_institutional_respects_tighter_thresholds():
    strat = RenderQuantV2_Institutional()
    strat.dp = MagicMock()
    strat.dp.runmode = "backtest"

    df = _base_df(3)
    df["ker"] = 0.32  # bloqueado na V2 trend (<0.36)
    df["bb_pos"] = [0.20, 0.03, 0.035]  # bloqueado na V2 rev (>0.02)
    df["rsi"] = [35.0, 29.0, 29.0]
    out = strat.populate_entry_trend(df, {"pair": "ETH/USDT"})
    assert out.get("enter_long", pd.Series([0] * 3)).iloc[2] != 1


def test_v1_exit_does_not_dump_below_sma50_and_requires_profit():
    strat = RenderQuantV1()
    assert strat.exit_profit_only is True
    assert getattr(strat, "exit_profit_min", 0.0) >= 0.010

    df = pd.DataFrame({
        "close": [90.0, 95.0, 102.0],
        "sma50": [100.0, 100.0, 100.0],
        "bb_middle": [99.0, 99.0, 100.0],
        "rsi": [32.0, 50.0, 66.0],
        "volume": [100.0, 100.0, 100.0],
    })
    out = strat.populate_exit_trend(df, {"pair": "ETH/USDT"})
    exit_col = out.get("exit_long", pd.Series([0] * 3))
    assert exit_col.iloc[0] != 1
    assert exit_col.iloc[1] != 1
    assert exit_col.iloc[2] == 1


def test_cmd_why_renders_v1_timeframe_15m(tmp_path):
    why_json = json.dumps({
        "strategy": "RenderQuantV1",
        "timeframe": "15m",
        "bb_pos_target": 0.04,
        "updated_at": "24/11/2026 23:55 UTC",
        "decision": "REJECT",
        "pair": "ETH/USDT",
        "ts": "02:15:24 UTC",
        "reason": "Regime Macro: BTC abaixo da média de 50 ($59179 < $59826)",
        "pairs": {
            "BTC/USDT": {"price": 59797.9, "bb_pos": 0.428, "rsi": 45.8},
            "ADA/USDT": {"price": 0.1444, "bb_pos": 0.553, "rsi": 48.6},
        },
    })

    msg_mock = MagicMock()
    msg_mock.reply_text = AsyncMock()
    upd = MagicMock()
    upd.effective_chat.id = 123456
    upd.effective_user.id = 123456
    upd.effective_message = msg_mock

    captured = {}

    class DummyApp:
        def add_handler(self, h, *args, **kwargs):
            if hasattr(h, "cmd"):
                captured[h.cmd] = h.fn

    class DummyCmd:
        def __init__(self, cmd, fn, *args, **kwargs):
            self.cmd = cmd
            self.fn = fn

    class DummyIKB:
        def __init__(self, text, *args, **kwargs):
            self.text = text

    class DummyTg:
        def __init__(self, *args, **kwargs):
            pass

        async def _send_msg(self, *args, **kwargs):
            pass

        async def _update_msg(self, *args, **kwargs):
            pass

    mod_ft_tg = types.ModuleType("freqtrade.rpc.telegram")
    mod_ft_tg.Telegram = DummyTg
    mod_tg = types.ModuleType("telegram")
    mod_tg.InlineKeyboardButton = DummyIKB
    mod_tg.InlineKeyboardMarkup = MagicMock()
    mod_tg_ext = types.ModuleType("telegram.ext")
    mod_tg_ext.Application = DummyApp
    mod_tg_ext.ApplicationHandlerStop = Exception
    mod_tg_ext.CallbackQueryHandler = lambda *a, **k: MagicMock()
    mod_tg_ext.CommandHandler = DummyCmd
    mod_tg_ext.MessageHandler = lambda *a, **k: MagicMock()
    mod_tg_ext.filters = MagicMock()

    orig_exists = pathlib.Path.exists
    orig_read_text = pathlib.Path.read_text

    def fake_exists(p_self):
        if p_self.name == "why_state.json":
            return True
        return orig_exists(p_self)

    def fake_read_text(p_self, *args, **kwargs):
        if p_self.name == "why_state.json":
            return why_json
        return orig_read_text(p_self, *args, **kwargs)

    with (
        patch.dict(
            sys.modules,
            {
                "freqtrade.rpc.telegram": mod_ft_tg,
                "telegram": mod_tg,
                "telegram.ext": mod_tg_ext,
            },
        ),
        patch.object(start_bot, "CHAT_ID", "123456"),
        patch.object(pathlib.Path, "exists", fake_exists),
        patch.object(pathlib.Path, "read_text", fake_read_text),
    ):
        start_bot.install_pt_br_hooks()
        mod_tg_ext.Application().add_handler(MagicMock())
        asyncio.run(captured["why"](upd, MagicMock()))
        sent = msg_mock.reply_text.call_args[0][0]
        assert "RenderQuantV1 (15m)" in sent
        assert "$0.1444" in sent
        assert "$59,797.90" in sent
        assert "Último Veto de Entrada" in sent
        assert "ETH/USDT" in sent


def test_record_decision_preserves_pairs_and_throttles():
    strat = RenderQuantV1()
    strat._why_pairs_cache = {
        "BTC/USDT": {"price": 59000.0, "bb_pos": 0.22, "rsi": 43.0},
    }
    captured_payloads = []

    def fake_save(key, payload, paths):
        captured_payloads.append((key, payload))

    with patch("user_data.state_store.save_state_async", side_effect=fake_save):
        strat._record_decision("ETH/USDT", "REJECT", "BTC Macro Veto")
        strat._record_decision("ETH/USDT", "REJECT", "BTC Macro Veto")

    assert len(captured_payloads) == 1
    _, payload = captured_payloads[0]
    assert "pairs" in payload
    assert "BTC/USDT" in payload["pairs"]
    assert payload["decision"] == "REJECT"
    assert payload["pair"] == "ETH/USDT"


def test_btc_macro_guard_allows_early_recovery_and_blocks_dump():
    from datetime import UTC, datetime

    strat = RenderQuantV1()
    strat.wallets = MagicMock()
    strat.wallets.get_total_stake_amount.return_value = 100.0
    strat.wallets.get_available_stake_amount.return_value = 100.0
    strat.config = {"stake_currency": "USDT", "runmode": "backtest"}
    strat.dp = MagicMock()
    strat.dp.runmode = "backtest"
    strat.dp.ticker.return_value = {"bid": 1650.0, "ask": 1650.5}

    # 1. BTC em queda forte abaixo da SMA50 (-1%) -> BLOQUEIA altcoin
    strat.dp.get_analyzed_dataframe.return_value = (
        pd.DataFrame([{
            "close": 59179.0,
            "sma50": 59826.0,
            "ema9": 59100.0,
            "ema21": 59300.0,
            "macd_hist": -50.0,
        }]),
        datetime.now(UTC),
    )
    ok_dump = strat.confirm_trade_entry(
        "ETH/USDT", "limit", 0.002, 1650.0, "gtc", datetime.now(UTC), "V1:oversold_reclaim", "long"
    )
    assert ok_dump is False

    # 2. BTC em recuperação inicial dentro de 0.5% da SMA50 (ema9>ema21 e macd_hist>0) -> LIBERA altcoin
    strat.dp.get_analyzed_dataframe.return_value = (
        pd.DataFrame([{
            "close": 59600.0,
            "sma50": 59800.0,
            "ema9": 59580.0,
            "ema21": 59450.0,
            "macd_hist": 25.0,
        }]),
        datetime.now(UTC),
    )
    ok_rec = strat.confirm_trade_entry(
        "ETH/USDT", "limit", 0.002, 1650.0, "gtc", datetime.now(UTC), "V1:early_cross", "long"
    )
    assert ok_rec is True
