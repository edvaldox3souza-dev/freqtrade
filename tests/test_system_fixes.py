"""Testes de regressão para as correções de sistema em start_bot, state_store, shadow_tracker e estratégias."""
import asyncio
import json
import pathlib
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

for mod in ["talib", "talib.abstract", "freqtrade", "freqtrade.persistence", "freqtrade.strategy"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()


class MockIStrategy:
    pass


sys.modules["freqtrade.strategy"].IStrategy = MockIStrategy

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import start_bot
from user_data import state_store
from user_data.strategies.RenderQuantV1 import RenderQuantV1
from user_data.strategies.RenderQuantV3_Defensive import RenderQuantV3_Defensive
from user_data.strategies.RenderQuantV4_Adaptive import RenderQuantV4_Adaptive


def test_ensure_runtime_config_respeita_dry_run_do_exchange_state(tmp_path, monkeypatch):
    src = tmp_path / "src.json"
    dst = tmp_path / "dst.json"
    src.write_text(
        json.dumps({"dry_run": False, "exchange": {"name": "kraken", "key": "k", "secret": "s"}}),
        encoding="utf-8",
    )
    monkeypatch.delenv("FREQTRADE__DRY_RUN", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)

    with (
        patch("user_data.state_store.load_state", return_value={"dry_run": True}),
        patch("user_data.state_store.save_state"),
        patch.object(start_bot, "get_strategy_state", return_value={"active_strategy": "RenderQuantV3_Defensive"}),
    ):
        out = start_bot.ensure_runtime_config(src_path=src, dst_path=dst)
        cfg = json.loads(out.read_text(encoding="utf-8"))
        assert cfg["dry_run"] is True


def test_state_store_cache_em_memoria_evita_db_repetido(tmp_path):
    state_store.clear_state_cache()
    f = tmp_path / "st.json"
    with patch("user_data.state_store.db_get", return_value={"enabled": True}) as m_get:
        s1 = state_store.load_state("k_test", [f], {"enabled": False}, cache_ttl_secs=5.0)
        s2 = state_store.load_state("k_test", [f], {"enabled": False}, cache_ttl_secs=5.0)
        assert s1["enabled"] is True and s2["enabled"] is True
        assert m_get.call_count == 1


def test_v3_e_v4_nao_falham_com_current_time_naive_vs_close_date_aware():
    mock_trade = MagicMock()
    mock_trade.close_date_utc = datetime.now(UTC)
    mock_trade.close_profit_abs = 0.0

    for cls in (RenderQuantV3_Defensive, RenderQuantV4_Adaptive):
        strat = cls()
        strat.wallets = MagicMock()
        strat.wallets.get_total_stake_amount.return_value = 1000.0
        strat.dp = None
        naive_now = datetime.now()  # sem tzinfo
        with (
            patch("user_data.strategies.RenderQuantV3_Defensive.Trade.get_trades_proxy", side_effect=[[mock_trade], []]),
            patch("user_data.strategies.RenderQuantV4_Adaptive.Trade.get_trades_proxy", side_effect=[[mock_trade], []]),
            patch("user_data.ai_guardian.get_state", return_value={"enabled": False}),
        ):
            ok = strat.confirm_trade_entry("BTC/USDT", "limit", 0.0005, 60000.0, "gtc", naive_now, "tag", "long")
            assert ok is True


def test_v1_custom_exit_ignora_ia_em_backtest():
    strat = RenderQuantV1()
    strat.dp = MagicMock()
    strat.dp.runmode = "backtest"
    mock_trade = MagicMock()
    mock_trade.open_date_utc = datetime.now(UTC)
    with patch("user_data.ai_guardian.consult_ai_exit") as m_exit:
        res = strat.custom_exit("BTC/USDT", mock_trade, datetime.now(UTC), 62000.0, 0.03)
        assert res is None
        assert m_exit.call_count == 0


def test_comandos_telegram_bloqueiam_chat_nao_autorizado():
    """Garante que _cmd_start_panel, _cmd_corretora e _cmd_ai_guardian bloqueiem chats estranhos."""
    import types

    captured_handlers = []

    class DummyTelegram:
        def __init__(self, *args, **kwargs):
            pass

        async def _send_msg(self, *args, **kwargs):
            pass

        async def _update_msg(self, *args, **kwargs):
            pass

    class DummyIKB:
        def __init__(self, text, *args, **kwargs):
            self.text = text

    class DummyApp:
        def add_handler(self, handler, *args, **kwargs):
            captured_handlers.append(handler)

    class DummyCmdHandler:
        def __init__(self, cmd, fn, *args, **kwargs):
            self.cmd = cmd
            self.fn = fn

    class DummyCbHandler:
        def __init__(self, fn, *args, **kwargs):
            self.fn = fn

    class DummyMsgHandler:
        def __init__(self, filt, fn, *args, **kwargs):
            self.fn = fn

    mod_ft_tg = types.ModuleType("freqtrade.rpc.telegram")
    mod_ft_tg.Telegram = DummyTelegram
    mod_tg = types.ModuleType("telegram")
    mod_tg.InlineKeyboardButton = DummyIKB
    mod_tg.InlineKeyboardMarkup = MagicMock()
    mod_tg_ext = types.ModuleType("telegram.ext")
    mod_tg_ext.Application = DummyApp
    mod_tg_ext.ApplicationHandlerStop = Exception
    mod_tg_ext.CallbackQueryHandler = DummyCbHandler
    mod_tg_ext.CommandHandler = DummyCmdHandler
    mod_tg_ext.MessageHandler = DummyMsgHandler
    mod_tg_ext.filters = MagicMock()

    with (
        patch.dict(sys.modules, {
            "freqtrade.rpc.telegram": mod_ft_tg,
            "telegram": mod_tg,
            "telegram.ext": mod_tg_ext,
        }),
        patch.object(start_bot, "CHAT_ID", "123456"),
    ):
        start_bot.install_pt_br_hooks()
        mod_tg_ext.Application().add_handler(MagicMock())
        cmd_map = {h.cmd: h.fn for h in captured_handlers if isinstance(h, DummyCmdHandler)}
        for cmd_name in ("start", "corretora", "ia"):
            assert cmd_name in cmd_map
            bad_update = MagicMock()
            bad_update.effective_chat.id = 999999999
            bad_update.effective_user.id = 999999999
            bad_update.message.reply_text = AsyncMock()
            bad_update.effective_chat.send_message = AsyncMock()
            asyncio.run(cmd_map[cmd_name](bad_update, MagicMock()))
            assert bad_update.message.reply_text.call_count == 0
            assert bad_update.effective_chat.send_message.call_count == 0
