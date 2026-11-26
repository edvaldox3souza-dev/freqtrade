import pathlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

import start_bot


def _setup_mock_telegram():
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
            self.callback_data = kwargs.get("callback_data")
            self.url = kwargs.get("url")

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

    return mod_ft_tg, mod_tg, mod_tg_ext, captured_handlers


@pytest.mark.asyncio
async def test_cmd_teste_ordem_chat_nao_autorizado():
    """Garante que _cmd_teste_ordem rejeite chamadas de chats não autorizados."""
    mod_ft_tg, mod_tg, mod_tg_ext, captured = _setup_mock_telegram()

    with (
        patch.dict(sys.modules, {
            "freqtrade.rpc.telegram": mod_ft_tg,
            "telegram": mod_tg,
            "telegram.ext": mod_tg_ext,
        }),
        patch.object(start_bot, "CHAT_ID", "1560345442"),
    ):
        start_bot.install_pt_br_hooks()
        mod_tg_ext.Application().add_handler(MagicMock())
        cmd_map = {h.cmd: h.fn for h in captured if hasattr(h, "cmd")}
        assert "teste" in cmd_map

        bad_update = MagicMock()
        bad_update.effective_chat.id = 999999999
        bad_update.effective_message = MagicMock()
        bad_update.effective_chat.send_message = AsyncMock()

        await cmd_map["teste"](bad_update, MagicMock())
        bad_update.effective_chat.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_teste_ordem_executa_force_entry():
    """Valida fluxo de sucesso ao disparar ordem real mínima de teste."""
    mod_ft_tg, mod_tg, mod_tg_ext, captured = _setup_mock_telegram()

    mock_trade = MagicMock()
    mock_trade.id = 42
    mock_trade.open_rate = 0.1475
    mock_trade.stake_amount = 2.95
    mock_trade.amount = 20.0

    mock_rpc = MagicMock()
    mock_rpc._config = {"dry_run": False, "max_open_trades": 3}
    mock_rpc._rpc_trade_status.return_value = []
    mock_rpc._rpc_force_entry.return_value = mock_trade

    mock_tg = MagicMock()
    mock_tg._rpc = mock_rpc

    with (
        patch.dict(sys.modules, {
            "freqtrade.rpc.telegram": mod_ft_tg,
            "telegram": mod_tg,
            "telegram.ext": mod_tg_ext,
        }),
        patch.dict(start_bot.tg_ref, {"self": mock_tg}),
        patch.object(start_bot, "CHAT_ID", "1560345442"),
    ):
        start_bot.install_pt_br_hooks()
        mod_tg_ext.Application().add_handler(MagicMock())
        cmd_map = {h.cmd: h.fn for h in captured if hasattr(h, "cmd")}
        assert "teste" in cmd_map

        good_update = MagicMock()
        good_update.effective_chat.id = 1560345442
        good_update.effective_chat.send_message = AsyncMock()

        await cmd_map["teste"](good_update, MagicMock())

        # Verifica chamada oficial force_entry com ADA/USDT a mercado
        mock_rpc._rpc_force_entry.assert_called_once_with("ADA/USDT", None, order_type="market")
        # Mensagens enviadas no chat
        assert good_update.effective_chat.send_message.call_count >= 2
        calls = [str(call) for call in good_update.effective_chat.send_message.call_args_list]
        assert any("Ordem Real Executada com Sucesso" in c for c in calls)


def test_telegram_menu_contains_teste_command():
    """Valida se o comando teste e botões estão no teclado e menu inline."""
    kb = start_bot.PT_BR_KEYBOARD
    assert any("🧪 Testar Ordem Real" in row for row in kb)

    markup = start_bot.build_main_menu_inline_markup()
    button_datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "unk_nav:teste" in button_datas
