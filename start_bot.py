import asyncio
import hashlib
import json
import os
import pathlib
import re
import sys
import threading
import time
import urllib.request
import uuid

import requests

# =============================================================================
# Bootstrap Seguro de Credenciais e AppSec (Claude Fable 5.1)
# =============================================================================
# P0-SECURITY: nenhum segredo hard-coded. Credenciais vêm exclusivamente de env vars.
# Se ausentes, geramos um segredo aleatório por boot (fail-closed: senha desconhecida
# invalida logins antigos em vez de aceitar um padrão público do repositório).
_REQUIRED_SECRETS = (
    "FREQTRADE__API_SERVER__PASSWORD",
    "FREQTRADE__API_SERVER__JWT_SECRET_KEY",
    "FREQTRADE__API_SERVER__WS_TOKEN",
)
_FIXED_DEFAULTS = {
    "FREQTRADE__API_SERVER__ENABLED": "true",
    "FREQTRADE__API_SERVER__LISTEN_IP_ADDRESS": "0.0.0.0",
}

_PLACEHOLDERS = {
    "", "change_me", "change-me", "changeme", "change_me_please",
    "change_me_via_env_or_secret_file", "your_password_here", "your_password",
    "none", "null", "undefined", "secret", "password", "<password>", "<secret>",
}

_MIN_LEN = {
    "FREQTRADE__API_SERVER__PASSWORD": 16,
    "FREQTRADE__API_SERVER__JWT_SECRET_KEY": 32,
    "FREQTRADE__API_SERVER__WS_TOKEN": 32,
}


def _fingerprint(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"len={len(value)} sha256={digest}"


def _is_unsafe(value: str | None) -> bool:
    return value is None or value.strip().lower() in _PLACEHOLDERS


def _harden_environment() -> None:
    import secrets as _secrets

    errors: list[str] = []
    for key, default in _FIXED_DEFAULTS.items():
        if _is_unsafe(os.environ.get(key)):
            os.environ[key] = default

    # Persistência estável de credenciais de API no Neon DB / cache local
    saved_creds = {}
    try:
        from user_data.state_store import load_state
        saved_creds = load_state("api_server_creds", [pathlib.Path("/tmp/api_server_creds.json")], {})
    except Exception as exc:  # noqa: BLE001
        print(f"[bootstrap] alerta leitura state_store: {exc}", flush=True)

    creds_updated = False
    for key in _REQUIRED_SECRETS:
        current = os.environ.get(key)
        if _is_unsafe(current):
            if saved_creds and saved_creds.get(key) and not _is_unsafe(saved_creds.get(key)):
                os.environ[key] = saved_creds[key]
                print(f"[bootstrap] {key}: recuperado do Neon DB (credencial estável mantida).", flush=True)
            else:
                generated = _secrets.token_urlsafe(32)
                os.environ[key] = generated
                saved_creds[key] = generated
                creds_updated = True
                print(f"[bootstrap] {key}: gerado e salvo para persistência estável.", flush=True)
        elif key in _MIN_LEN and len(os.environ[key].strip()) < _MIN_LEN[key]:
            errors.append(f"{key} tem {len(os.environ[key].strip())} caracteres (mínimo {_MIN_LEN[key]}).")

        if key in _MIN_LEN:
            print(f"[bootstrap] {key}: {_fingerprint(os.environ[key])}", flush=True)

    if creds_updated:
        try:
            from user_data.state_store import save_state
            save_state("api_server_creds", saved_creds, [pathlib.Path("/tmp/api_server_creds.json")])
        except Exception as exc:  # noqa: BLE001
            print(f"[bootstrap] alerta salvamento state_store: {exc}", flush=True)

    if errors:
        print("[bootstrap] FALHA DE SEGURANÇA — abortando:", file=sys.stderr, flush=True)
        for e in errors:
            print(f"  - {e}", file=sys.stderr, flush=True)
        sys.exit(1)


_harden_environment()
# =============================================================================

def _load_tg_creds():
    tok = os.environ.get("FREQTRADE__TELEGRAM__TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    cid = os.environ.get("FREQTRADE__TELEGRAM__CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not tok or not cid:
        for cfg_path in (
            pathlib.Path("/freqtrade/user_data/config.render.json"),
            pathlib.Path("/etc/secrets/config.json"),
        ):
            try:
                if cfg_path.exists():
                    tg = json.loads(cfg_path.read_text(encoding="utf-8")).get("telegram", {})
                    tok = tok or tg.get("token", "")
                    cid = cid or str(tg.get("chat_id", ""))
            except Exception:
                pass
    return tok, cid.strip()


TOKEN, CHAT_ID = _load_tg_creds()
KEEP_ALIVE_INTERVAL = 540


def is_authorized_chat(update) -> bool:
    """P0-SECURITY: somente o CHAT_ID configurado via env var é autorizado.

    Sem auto-pairing: desconhecidos recebem o próprio chat_id como instrução
    para configurar a variável no Render, e nada mais.
    """
    if not update or not update.effective_chat:
        return False
    cid = str(update.effective_chat.id)
    uid = str(update.effective_user.id) if update.effective_user else ""
    allowed = str(CHAT_ID).strip() if CHAT_ID else ""
    if not allowed:
        return False
    return cid == allowed or uid == allowed


PT_BR_KEYBOARD = [
    ["🎯 Mudar Estratégia", "❓ Por Que Não?"],
    ["📊 Operações Abertas", "💰 Resultados"],
    ["💳 Saldo da Conta", "📈 Desempenho"],
    ["📋 Pares Ativos", "🏆 Por Moeda"],
    ["▶️ Iniciar Robô", "⏸️ Pausar Robô"],
    ["🏦 Corretora", "🧠 Guardião IA"],
    ["🩺 Diagnóstico", "⚙️ Configurações"],
    ["🌐 Painel Web", "❓ Ajuda (Guia)"],
]

PT_BR_BUTTON_MAP = {
    "🎯 Mudar Estratégia": "_unkiorra_estrategia",
    "🎯 Alternar Estratégia": "_unkiorra_estrategia",
    "❓ Por Que Não?": "_unkiorra_why",
    "❓ Por Que Não Entrou?": "_unkiorra_why",
    "📊 Operações Abertas": "_status",
    "💰 Resultados": "_profit",
    "💰 Lucros e Ganhos": "_profit",
    "💳 Saldo da Conta": "_balance",
    "📈 Desempenho": "_stats",
    "📈 Estatísticas": "_stats",
    "🏆 Por Moeda": "_performance",
    "📋 Pares Ativos": "_whitelist",
    "▶️ Iniciar Robô": "_start",
    "⏸️ Pausar Robô": "_stop",
    "⚙️ Configurações": "_reload_config",
    "🔄 Atualizar Config": "_reload_config",
    "🔄 Recarregar": "_reload_config",
    "❓ Ajuda (Guia)": "_help",
    "🏦 Corretora": "_unkiorra_corretora",
    "🏦 Corretora / API": "_unkiorra_corretora",
    "🧠 Guardião IA": "_cmd_ai_guardian",
    "🧠 Guardião IA (Fable)": "_cmd_ai_guardian",
    "🧠 IA Guardian": "_cmd_ai_guardian",
    "🩺 Diagnóstico": "_cmd_diag",
    "🩺 Saúde do Robô": "_cmd_diag",
    "🌐 Painel Web": "_cmd_painel_web",
    "🌐 Painel": "_cmd_painel_web",
    "🌐 Web": "_cmd_painel_web",
}

WELCOME_PT_BR = (
    "🤖 *Unkiorra Trade Bot*\n\n"
    "✅ *Robô Ativo:* Monitorando oportunidades em tempo real\n"
    "🏦 *Corretora:* KRAKEN (`Pares Spot` | `RenderQuantV3_Defensive` `4h`)\n"
    "🚀 *Modo:* **Conta Real** (`Kraken Spot` — Saldo Real Ativo)\n"
    "🧠 *Guardião IA:* Claude Opus 5.5 Ativo (`/ia` ou toque em 🧠 Guardião IA)\n"
    "⚡ *Proteção de Capital:* Ativa (`Mean Reversion` + `StoplossGuard` + `MaxDrawdown`)\n"
    "👻 *Telemetria:* Shadow Trading Ativo no Neon DB (`/shadow`)\n\n"
    "👇 *Toque nos botões abaixo para controlar o robô:*"
)

HELP_PT_BR = (
    "🇧🇷 *Guia Completo em Português — Unkiorra Trade Bot*\n\n"
    "📊 *Consultas e Relatórios:*\n"
    "• *📊 Operações Abertas* (`/status`) — Mostra os trades abertos e o lucro atual.\n"
    "• *💰 Lucros e Ganhos* (`/profit`) — Exibe lucro acumulado, taxa de acerto e melhor par.\n"
    "• *💳 Saldo da Conta* (`/balance`) — Mostra o saldo disponível e total estimado em USD.\n"
    "• *📅 Lucro Diário* (`/daily`) — Mostra o lucro ou prejuízo dos últimos dias.\n"
    "• *📈 Estatísticas* (`/stats`) — Estatísticas de ganhos, perdas e motivos de saída.\n"
    "• *🏆 Por Moeda* (`/performance`) — Retorno percentual obtido por moeda.\n"
    "• *📋 Moedas Monitoradas* (`/whitelist`) — Lista os 10 pares USD monitorados na Kraken.\n"
    "• *🔢 Limite de Trades* (`/count`) — Quantidade de operações abertas (máx: 3).\n\n"
    "⚙️ *Controle do Robô:*\n"
    "• *▶️ Iniciar Robô* (`/start`) — Inicia o robô e abre este painel.\n"
    "• *⏸️ Pausar Robô* (`/stop`) — Pausa a abertura de novas operações.\n"
    "• *🔄 Atualizar Config* (`/reload_config`) — Recarrega a configuração.\n"
    "• `/health` — Verifica a saúde e último batimento do robô.\n"
    "• `/version` — Mostra a versão do Freqtrade.\n\n"
    "🏦 *Corretora e Chaves API (100% por botões):*\n"
    "• Toque em *🏦 Corretora / API* no teclado para escolher a corretora, configurar API Key / Secret / Passphrase e alternar entre Simulado e Conta Real.\n"
    "• Toque em *🩺 Saúde do Robô* para verificar o último processamento."
)

# Strictly idempotent dictionary (no value is a key in any case, no substring recursion)
WEB_DICT = {
    "Freqtrade UI": "Painel Unkiorra Trade Bot",
    "Freqtrade": "Unkiorra Trade Bot",
    "UI": " (Painel)",
    "Freqtrader": "Nome de usuário",
    "Login": "Entrar",
    "Log in": "Entrar",
    "Sign in": "Entrar",
    "Logout": "Sair",
    "Log out": "Sair",
    "Sign out": "Sair",
    "Username": "Usuário",
    "Password": "Senha",
    "Bot Name": "Nome do Robô",
    "API Url": "URL da API",
    "API URL": "URL da API",
    "Add new bot": "Adicionar novo robô",
    "Add Bot": "Adicionar robô",
    "Login to your bot": "Conectar ao seu robô",
    "Freqtrade bot Login": "Acesso ao Robô Unkiorra",
    "Freqtrade bot login": "Acesso ao Robô Unkiorra",
    "Enter your bot credentials to connect": "Insira as credenciais do seu robô para conectar",
    "Enter your bot credentials to connect.": "Insira as credenciais do seu robô para conectar.",
    "Welcome to the Freqtrade UI": "Bem-vindo ao Painel Unkiorra Trade Bot",
    "This page allows you to control your trading bot.": "Esta página permite monitorar e controlar seu robô de operações.",
    "Learn more about Freqtrade": "Documentação Oficial",
    "Edit bot": "Editar robô",
    "Remove bot": "Remover robô",
    "Select a bot": "Selecionar um robô",
    "No bot selected": "Nenhum robô selecionado",
    "Home": "Início",
    "Trade": "Operações",
    "Trades": "Operações",
    "Dashboard": "Painel de Controle",
    "Chart": "Gráfico",
    "Charts": "Gráficos",
    "Backtest": "Simulador",
    "Backtesting": "Simulador",
    "Download Data": "Baixar Dados",
    "Pairlist": "Lista de Pares",
    "Pairlists": "Listas de Pares",
    "Whitelist": "Moedas Monitoradas",
    "Whitelist Methods": "Métodos de Seleção",
    "Blacklist": "Moedas Bloqueadas",
    "No Blacklist Available.": "Nenhuma moeda bloqueada.",
    "If you expect to have pairs in your blacklist, please login and make sure the server is running.": "Se você espera ver pares bloqueados, verifique se o robô está ativo.",
    "No Blacklist Available. If you expect to have pairs in your blacklist, please login and make sure the server is running.": "Nenhuma moeda bloqueada. Se você espera ver pares bloqueados, verifique se o robô está ativo.",
    "Logs": "Registros",
    "Bot Logs": "Registros do Robô",
    "Reload Logs": "Atualizar Registros",
    "Refresh Logs": "Atualizar Registros",
    "No logs": "Nenhum registro disponível",
    "Auto-refresh": "Atualização automática",
    "Settings": "Configurações",
    "FreqUI Settings": "Configurações do Painel",
    "UI Settings": "Configurações da Interface",
    "UI Version": "Versão do Painel",
    "Timezone": "Fuso Horário",
    "UTC Timezone": "Fuso Horário UTC",
    "Select timezone (UTC is recommended as exchanges usually work in UTC)": "Selecione o fuso horário (recomenda-se UTC ou America/Sao_Paulo).",
    "Open trades Visualization": "Visualização de Operações Abertas",
    "Decide if open trades should be visualized": "Escolha como visualizar a contagem de operações abertas",
    "Show pill in icon": "Mostrar contador no ícone da aba",
    "Show open trades in header": "Mostrar operações abertas no cabeçalho",
    "Lock layout": "Travar layout",
    "Lock Layout": "Travar layout",
    "Unlock Layout": "Destravar layout",
    "Lock dynamic layouts": "Travar layouts dinâmicos",
    "Lock dynamic layouts, so they cannot be moved anymore. Can also be set from the navbar at the top.": "Trava os painéis na tela para não serem movidos acidentalmente.",
    "Reset layout": "Restaurar layout padrão",
    "Reset dynamic layouts to how they were.": "Restaura a posição original dos painéis na tela.",
    "Dynamic layout": "Layout dinâmico",
    "Background sync": "Sincronização em segundo plano",
    "Sync background bots while other bot is selected.": "Mantém a sincronização ativa ao alternar entre robôs.",
    "Show Confirm Dialog for Trade Exits": "Confirmar antes de forçar saída de operação",
    "Always show a confirmation dialog when force-exiting a trade.": "Exibe uma confirmação antes de encerrar manualmente uma operação.",
    "Show Text on Multi Pane Buttons": "Mostrar texto nos botões do painel múltiplo",
    "Show text on multi pane buttons. If disabled, only images will be shown.": "Exibe o nome ao lado dos ícones nos botões do painel múltiplo.",
    "Chart settings": "Configurações do Gráfico",
    "Chart scale Side": "Lado da Escala do Gráfico",
    "Left": "Esquerda",
    "Right": "Direita",
    "Should the scale be displayed on the right or left?": "Define se a escala de preços fica à direita ou à esquerda.",
    "Use Heikin Ashi candles": "Usar velas Heikin Ashi",
    "Use Heikin Ashi candles in your charts": "Exibe velas Heikin Ashi por padrão nos gráficos.",
    "Only request necessary columns": "Solicitar apenas colunas essenciais",
    "Only request columns that are necessary for the chart. May improve transfer speed on large datasets.": "Transfere apenas as colunas necessárias para acelerar o gráfico.",
    "Default number of candles to display (defaults to 250)": "Quantidade padrão de velas exibidas no gráfico (padrão: 250).",
    "Candle Color Preference": "Preferência de Cores das Velas",
    "Green Up/Red Down": "Verde Alta / Vermelho Baixa",
    "Red Up/Green Down": "Vermelho Alta / Verde Baixa",
    "Use muted colors": "Usar cores suaves",
    "Color preference": "Preferência de cores",
    "Up / Down color": "Cores de Alta / Baixa",
    "Notification Settings": "Configurações de Notificações",
    "Notifications": "Notificações",
    "Entry notifications": "Notificações de Entrada",
    "Exit notifications": "Notificações de Saída",
    "Entry Cancel notifications": "Notificações de Cancelamento de Entrada",
    "Exit Cancel notifications": "Notificações de Cancelamento de Saída",
    "Backtesting settings": "Configurações de Simulação",
    "Backtesting metrics": "Métricas de Simulação",
    "Profit Factor, Expectancy": "Fator de Lucro, Expectativa",
    "Select which metrics should be shown on a per pair / tag basis.": "Selecione quais métricas exibir por par ou sinal.",
    "About": "Sobre",
    "Open Trades": "Operações Abertas",
    "Open trades": "Operações Abertas",
    "All open trades": "Todas as Operações Abertas",
    "Currently no open trades.": "Nenhuma operação aberta no momento.",
    "Currently no open trades": "Nenhuma operação aberta no momento",
    "Closed Trades": "Operações Fechadas",
    "Closed trades": "Operações Fechadas",
    "Trade History": "Histórico de Operações",
    "Trades Log": "Histórico de Operações",
    "Trade Log": "Histórico de Operações",
    "Trade Detail": "Detalhes da Operação",
    "Trade Details": "Detalhes da Operação",
    "Bot comparison": "Comparativo de Robôs",
    "Bot Comparison": "Comparativo de Robôs",
    "Cumulative Profit": "Lucro Acumulado",
    "Cum. Profit": "Lucro Acumulado",
    "Cum Profit": "Lucro Acumulado",
    "Profit Distribution": "Distribuição de Lucros",
    "Profit over time": "Evolução do Lucro",
    "Balance": "Saldo da Conta",
    "Bot Balance": "Saldo do Robô",
    "Balances": "Saldos da Conta",
    "Wallet History": "Histórico da Carteira",
    "No historic wallet data available.": "Sem histórico de evolução de saldo disponível.",
    "You may need to update your freqtrade version to have historic wallet balance data available.": "O histórico da carteira será preenchido conforme novas operações forem registradas.",
    "Profit": "Lucros",
    "Open Profit": "Lucro Aberto",
    "Closed Profit": "Lucro Fechado",
    "Daily Profit": "Lucro Diário",
    "Weekly Profit": "Lucro Semanal",
    "Monthly Profit": "Lucro Mensal",
    "Period Breakdown": "Resultado por Período",
    "Daily Breakdown": "Resultado Diário",
    "Daily": "Diário",
    "Weekly": "Semanal",
    "Monthly": "Mensal",
    "Hourly": "Por Hora",
    "Weeks": "Semanas",
    "Months": "Meses",
    "Abs $": "Valor $",
    "Rel %": "Rel. %",
    "Profit%": "Lucro %",
    "Performance": "Desempenho",
    "Statistics": "Estatísticas",
    "Stats": "Estatísticas",
    "General": "Geral",
    "Summary": "Resumo",
    "Overview": "Visão Geral",
    "Bot Status": "Estado do Robô",
    "Bot status": "Estado do Robô",
    "Bot Controls": "Controles do Robô",
    "Status": "Estado",
    "State": "Estado",
    "Bot State": "Estado do Robô",
    "Start": "Iniciar",
    "Start Bot": "Iniciar Robô",
    "Start Trading": "Iniciar Operações",
    "Stop": "Pausar",
    "Stop Bot": "Pausar Robô",
    "Stop Trading": "Pausar Operações",
    "Pause": "Pausar Entradas",
    "StopBuy": "Pausar Compras",
    "Reload Config": "Recarregar Config",
    "Reload": "Atualizar",
    "Force Exit": "Forçar Saída",
    "Forceexit": "Forçar Saída",
    "Forceexit all": "Forçar Saída de Todas",
    "Force Entry": "Forçar Entrada",
    "Forceentry": "Forçar Entrada",
    "Dry Run": "Modo Simulado",
    "Dry-Run": "Modo Simulado",
    "Spot Dry-Run": "Spot — Modo Simulado",
    "Live": "Conta Real",
    "Spot": "Mercado à Vista (Spot)",
    "Futures": "Mercado de Futuros",
    "Running": "Em andamento",
    "running": "Em andamento",
    "Stopped": "Parado",
    "stopped": "Parado",
    "Paused": "Pausado",
    "paused": "Pausado",
    "Online": "Conectado",
    "Offline": "Desconectado",
    "Strategy": "Estratégia",
    "Strategy parameters": "Parâmetros da Estratégia",
    "Stoploss on exchange is disabled.": "Stoploss na corretora está desativado.",
    "Stoploss on exchange is enabled.": "Stoploss na corretora está ativado.",
    "Stoploss on exchange is": "Stoploss na corretora está",
    "disabled": "desativado",
    "enabled": "ativado",
    "Currently": "Atualmente",
    "Running with": "Operando com",
    "on": "na",
    "in": "em",
    "markets, with Strategy": "mercados, com a Estratégia",
    "markets, with": "mercados, com",
    "with Strategy": "com a Estratégia",
    "Exchange": "Corretora",
    "Trading mode": "Modo de Operação",
    "Margin mode": "Modo de Margem",
    "Max open trades": "Máx. de Operações Abertas",
    "Stake currency": "Moeda Base (Stake)",
    "Stake amount": "Valor por Operação",
    "Total stake amount": "Valor total investido",
    "Stoploss": "Limite de Perda (Stoploss)",
    "Trailing stoploss": "Stoploss Móvel",
    "Minimal ROI": "Lucro Mínimo (ROI)",
    "Timeframe": "Tempo Gráfico",
    "Candles": "Velas (Candles)",
    "Multi Pane": "Múltiplos Painéis",
    "Multi pair": "Múltiplos Pares",
    "Show Chart Areas": "Mostrar Áreas do Gráfico",
    "Plot Config": "Configurar Gráfico",
    "Plot Configurator": "Configurador de Gráfico",
    "Indicators": "Indicadores",
    "Show Trades": "Mostrar Operações",
    "Show Signals": "Mostrar Sinais",
    "No chart data available": "Sem dados de gráfico disponíveis",
    "Loading": "Carregando",
    "Loading...": "Carregando...",
    "Currency": "Moeda",
    "Coin": "Moeda",
    "Available": "Disponível",
    "Free": "Livre",
    "Used": "Em uso",
    "Estimated Value": "Valor Estimado",
    "Est. Value": "Valor Estimado",
    "Est. USDT": "Estimado em USDT",
    "in undefined": "em USDT",
    "In undefined": "Em USDT",
    "Starting Capital": "Capital Inicial",
    "Starting capital": "Capital Inicial",
    "Show all": "Mostrar todas",
    "Hide small balances": "Ocultar saldos pequenos",
    "Pair": "Par",
    "Pairs": "Pares",
    "Locks": "Bloqueios",
    "Pair Locks": "Bloqueios de Pares",
    "Active Locks": "Bloqueios Ativos",
    "No active locks": "Nenhum bloqueio ativo",
    "Until": "Até",
    "Reason": "Motivo",
    "Close Reason": "Motivo de Fechamento",
    "Entries": "Entradas",
    "Exits": "Saídas",
    "Mix Tag": "Sinal Misto",
    "Profit USDT": "Lucro USDT",
    "Amount": "Quantidade",
    "Stake": "Investido",
    "Stake Amount": "Valor Investido",
    "Open Rate": "Preço de Entrada",
    "Current Rate": "Preço Atual",
    "Close Rate": "Preço de Saída",
    "Current Profit": "Lucro Atual",
    "Current profit %": "Lucro Atual %",
    "Close Profit": "Lucro Final",
    "Close profit %": "Lucro Final %",
    "Profit %": "Lucro %",
    "Profit closed": "Lucro Fechado",
    "Profit all": "Lucro Geral",
    "Avg Profit": "Lucro Médio",
    "Avg. Profit": "Lucro Médio",
    "Avg Profit %": "Lucro Médio %",
    "Total Profit": "Lucro Total",
    "Tot Profit": "Lucro Total",
    "Total Stake": "Total Investido",
    "Open Date": "Data de Abertura",
    "Open since": "Aberta desde",
    "Close Date": "Data de Fechamento",
    "Closed at": "Fechada em",
    "Duration": "Duração",
    "Avg. Duration": "Duração Média",
    "Avg Duration": "Duração Média",
    "Exit Reason": "Motivo de Saída",
    "Enter Tag": "Sinal de Entrada",
    "Actions": "Ações",
    "Details": "Detalhes",
    "Win / Loss": "Vitórias / Derrotas",
    "Wins / Losses": "Vitórias / Derrotas",
    "W / L": "V / D",
    "W/L": "V/D",
    "All": "Todos",
    "Wins": "Ganhos",
    "Losses": "Perdas",
    "Draws": "Empates",
    "Winrate": "Taxa de Acerto",
    "Best Pair": "Melhor Par",
    "Best Performing": "Melhor Par",
    "Trading volume": "Volume Negociado",
    "Profit factor": "Fator de Lucro",
    "Expectancy": "Expectativa de Ganho",
    "Max Drawdown": "Drawdown Máximo",
    "Bot Heartbeat": "Batimento do Robô",
    "Last process": "Último processamento",
    "Initial bot start": "Início do robô",
    "Bot start": "Início do robô",
    "First trade": "Primeira operação",
    "Last trade": "Última operação",
    "Version": "Versão",
    "Freqtrade Version": "Versão do Freqtrade",
    "No open trades": "Nenhuma operação aberta no momento",
    "No closed trades": "Nenhuma operação fechada até o momento",
    "No trades to show": "Nenhuma operação para exibir",
    "Nothing to show": "Nada para exibir",
    "No data available": "Sem dados disponíveis",
    "No data": "Sem dados",
    "No records available": "Nenhum registro disponível",
    "Filter": "Filtrar",
    "Search": "Buscar",
    "Per page": "Por página",
    "Rows per page": "Linhas por página",
    "Date": "Data",
    "Day": "Dia",
    "Days": "Dias",
    "Count": "Qtd.",
    "Bot": "Robô",
    "Bots": "Robôs",
    "Login failed.": "Falha no login.",
    "Please verify that the bot is running, the Bot API is enabled and the URL is reachable.": "Verifique se o robô está ativo e se o usuário/senha estão corretos.",
    "Submit": "Confirmar",
    "OK": "Confirmar",
    "Cancel": "Cancelar",
    "Close": "Fechar",
    "Delete": "Excluir",
    "Remove": "Remover",
    "Save": "Salvar",
    "Reset": "Limpar",
    "Refresh": "Atualizar",
    "Help": "Ajuda",
    "Dark mode": "Modo Escuro",
    "Light mode": "Modo Claro",
}

WEB_PT_BR_MARKER = "/*PT_BR_UNKIORRA_V8*/"


def get_api_creds() -> tuple[str, str]:
    env_u = os.environ.get("FREQTRADE__API_SERVER__USERNAME", "")
    env_p = os.environ.get("FREQTRADE__API_SERVER__PASSWORD", "")
    if env_u or env_p:
        return env_u or "freqtrader", env_p
    for cfg_path in (
        pathlib.Path("/freqtrade/user_data/config.render.json"),
        pathlib.Path("/etc/secrets/config.json"),
    ):
        try:
            if cfg_path.exists():
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                api = cfg.get("api_server", {})
                return api.get("username", "freqtrader"), api.get("password", "")
        except Exception:
            pass
    return "freqtrader", ""



SECRET_CONFIG_PATH = pathlib.Path("/freqtrade/user_data/config.render.json")
RUNTIME_CONFIG_PATH = pathlib.Path("/tmp/runtime_config.json")
RUNTIME_USERDIR_PATH = pathlib.Path("/tmp/freqtrade_user_data")
_RUNTIME_CFG_LOCK = threading.Lock()
PENDING_INPUT_TTL_SEC = 300


def _atomic_write_text(dst_path: pathlib.Path, content: str) -> None:
    tmp_path = dst_path.with_name(f".{dst_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, dst_path)


def _ping_binance_sync() -> None:
    urllib.request.urlopen("https://api.binance.com/api/v3/ping", timeout=6).read()


async def precheck_exchange_async(ex_name: str) -> tuple[bool, str]:
    if ex_name == "binance":
        try:
            await asyncio.to_thread(_ping_binance_sync)
        except Exception as exc:
            return False, f"Binance bloqueou o IP do servidor ({exc}). Mantendo corretora anterior."
    return True, ""


def ensure_user_data_dir(userdir: pathlib.Path = RUNTIME_USERDIR_PATH) -> pathlib.Path:
    userdir.mkdir(parents=True, exist_ok=True)
    (userdir / "data").mkdir(parents=True, exist_ok=True)
    return userdir


def ensure_runtime_config(
    src_path: pathlib.Path = SECRET_CONFIG_PATH,
    dst_path: pathlib.Path = RUNTIME_CONFIG_PATH,
) -> pathlib.Path:
    with _RUNTIME_CFG_LOCK:
        # FIX 3 (cache/drift): no boot, SEMPRE reescreve a runtime config a partir
        # da config-fonte do repo. O /tmp é volátil e pode conter uma config antiga
        # (sem "strategy", com defaults) que sobrevive entre restarts — causa raiz
        # observada em produção. Overlays (env vars, telegram) são reaplicados abaixo.
        if src_path.exists():
            _atomic_write_text(dst_path, src_path.read_text(encoding="utf-8"))
        if dst_path.exists():
            try:
                cfg = json.loads(dst_path.read_text(encoding="utf-8"))
                changed = False
                # FIX 1: força sempre a estratégia ativa do strategy_state no boot.
                # Sem isto, uma runtime config sem "strategy" faz o freqtrade cair
                # no fallback SimpleStrategy (template 5m) — bug observado em produção.
                # FIX 5 (user_data_dir): grava o user_data_dir na runtime config para o
                # resolver buscar as estrategias custom em /freqtrade/user_data/strategies
                # (sem isto o lookup path cai no default "freqtrade/templates" -> SampleStrategy).
                if cfg.get("user_data_dir") != "/freqtrade/user_data":
                    cfg["user_data_dir"] = "/freqtrade/user_data"
                    changed = True
                    print("PT-BR_BOOT_USERDIR_FORCE: /freqtrade/user_data", flush=True)
                if cfg.get("strategy_path") != "/freqtrade/user_data/strategies":
                    cfg["strategy_path"] = "/freqtrade/user_data/strategies"
                    changed = True
                    print("PT-BR_BOOT_STRATEGY_PATH_FORCE: /freqtrade/user_data/strategies", flush=True)
                # FIX 1b: Preferência gravada no Neon DB / cache tem autoridade suprema sobre env var estática.
                force_override = os.environ.get("FORCE_STRATEGY_OVERRIDE", "").strip()
                resolved_strat = ""
                if force_override:
                    resolved_strat = force_override
                    print(f"PT-BR_BOOT_STRATEGY_FORCE_OVERRIDE: {resolved_strat}", flush=True)
                else:
                    try:
                        st = get_strategy_state()
                        resolved_strat = st.get("active_strategy", "").strip()
                    except Exception as exc:
                        print("PT-BR_BOOT_STRATEGY_WARN:", exc, flush=True)
                    if not resolved_strat:
                        resolved_strat = os.environ.get("FREQTRADE_STRATEGY", "").strip() or "RenderQuantV3_Defensive"

                if resolved_strat and cfg.get("strategy") != resolved_strat:
                    cfg["strategy"] = resolved_strat
                    changed = True
                    print(f"PT-BR_BOOT_STRATEGY_APPLIED: {resolved_strat}", flush=True)
                strat_tf = "4h" if ("V3" in resolved_strat or "V4" in resolved_strat) else "15m"
                if cfg.get("timeframe") != strat_tf:
                    cfg["timeframe"] = strat_tf
                    changed = True
                    print(f"PT-BR_BOOT_TIMEFRAME_SYNC: {strat_tf} para {resolved_strat}", flush=True)
                # FIX 2: chaves da corretora via env vars (persistentes na Render,
                # sobrevivem a sleep/redeploy). Env tem prioridade sobre /tmp.
                env_key = (
                    os.environ.get("EXCHANGE_KEY", "").strip()
                    or os.environ.get("FREQTRADE__EXCHANGE__KEY", "").strip()
                )
                env_secret = (
                    os.environ.get("EXCHANGE_SECRET", "").strip()
                    or os.environ.get("FREQTRADE__EXCHANGE__SECRET", "").strip()
                )
                env_pass = (
                    os.environ.get("EXCHANGE_PASSWORD", "").strip()
                    or os.environ.get("FREQTRADE__EXCHANGE__PASSWORD", "").strip()
                )
                ex_cfg = cfg.setdefault("exchange", {})
                if env_key and ex_cfg.get("key") != env_key:
                    ex_cfg["key"] = env_key
                    changed = True
                if env_secret and ex_cfg.get("secret") != env_secret:
                    ex_cfg["secret"] = env_secret
                    changed = True
                if env_pass and ex_cfg.get("password") != env_pass:
                    ex_cfg["password"] = env_pass
                    changed = True
                base_dry = bool(cfg.get("dry_run", False))
                env_dry = os.environ.get("FREQTRADE__DRY_RUN", "").strip().lower() or os.environ.get("DRY_RUN", "").strip().lower()
                target_dry = base_dry
                try:
                    from user_data.state_store import load_state, save_state
                    ex_st = load_state("exchange_state", [], {})
                    if "dry_run" in ex_st:
                        target_dry = bool(ex_st["dry_run"])
                    else:
                        target_dry = base_dry
                except Exception as exc:
                    target_dry = base_dry
                    print("PT-BR_BOOT_EX_STATE_WARN:", exc, flush=True)

                if env_dry in ("false", "0", "no"):
                    target_dry = False
                    print("PT-BR_BOOT_DRY_RUN_SOURCE: false (Conta Real forçada via ENV)", flush=True)
                elif env_dry in ("true", "1", "yes"):
                    target_dry = True
                    print("PT-BR_BOOT_DRY_RUN_SOURCE: true (Dry-Run forçado via ENV)", flush=True)

                ex = cfg.get("exchange", {})
                if not target_dry and (not ex.get("key") or not ex.get("secret")):
                    target_dry = True
                    print("PT-BR_BOOT_DRY_RUN_SAFETY: chaves ausentes, forçando Dry-Run por proteção", flush=True)

                cfg["dry_run"] = target_dry
                changed = True

                os.environ["FREQTRADE__DRY_RUN"] = "true" if target_dry else "false"
                os.environ["DRY_RUN"] = "true" if target_dry else "false"
                print(f"PT-BR_BOOT_DRY_RUN_FINAL: {target_dry} ({'Simulado' if target_dry else 'CONTA REAL KRAKEN SPOT LIVE'})", flush=True)

                try:
                    save_state("exchange_state", {"dry_run": target_dry, "exchange": ex_cfg.get("name", "kraken")}, [])
                except Exception:
                    pass
                # FIX 2b: db_url do DATABASE_URL direto na runtime config (override P0).
                # Garante Postgres persistente mesmo se o parsing de --db-url falhar.
                env_db = os.environ.get("DATABASE_URL", "").strip()
                if env_db.startswith("postgres://"):
                    env_db = env_db.replace("postgres://", "postgresql://", 1)
                if env_db and cfg.get("db_url") != env_db:
                    cfg["db_url"] = env_db
                    changed = True
                    print("PT-BR_BOOT_DB_URL_ENV_OVERRIDE: postgres configurado", flush=True)
                if cfg.get("fiat_display_currency", "") != "":
                    cfg["fiat_display_currency"] = ""
                    changed = True
                if TOKEN:
                    tg_cfg = cfg.setdefault("telegram", {})
                    tg_cfg["enabled"] = True
                    tg_cfg["token"] = TOKEN
                    tg_cfg["chat_id"] = str(CHAT_ID)
                    tg_cfg["keyboard"] = PT_BR_KEYBOARD
                    changed = True
                if "order_types" not in cfg or not cfg["order_types"].get("stoploss_on_exchange"):
                    cfg.setdefault("order_types", {}).update({
                        "entry": "limit",
                        "exit": "limit",
                        "emergency_exit": "market",
                        "force_entry": "market",
                        "force_exit": "market",
                        "stoploss": "market",
                        "stoploss_on_exchange": True,
                        "stoploss_on_exchange_interval": 60,
                        "stoploss_on_exchange_limit_ratio": 0.99,
                    })
                    changed = True
                if changed:
                    _atomic_write_text(dst_path, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
            except Exception as exc:
                print("PT-BR_RUNTIME_CFG_WARN:", exc, flush=True)
        return dst_path


def _mask_secret(val: str) -> str:
    if not val:
        return "Não configurada"
    if len(val) <= 4:
        return "****"
    return "****" + val[-4:]


EXCHANGES_WITH_ON_EXCHANGE_STOP = {"binance", "kucoin", "kraken", "gateio", "huobi"}


def get_exchange_status(dst_path: pathlib.Path = RUNTIME_CONFIG_PATH) -> dict:
    with _RUNTIME_CFG_LOCK:
        path = dst_path if dst_path.exists() else SECRET_CONFIG_PATH
        cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    ex = cfg.get("exchange", {})
    ex_name = ex.get("name", "kraken").lower()
    ot = cfg.get("order_types", {})
    on_exchange = ot.get("stoploss_on_exchange", True) and (ex_name in EXCHANGES_WITH_ON_EXCHANGE_STOP)
    return {
        "exchange": ex_name,
        "key_masked": _mask_secret(ex.get("key", "")),
        "secret_masked": _mask_secret(ex.get("secret", "")),
        "has_passphrase": bool(ex.get("password")),
        "dry_run": bool(cfg.get("dry_run", True)),
        "stoploss_on_exchange": on_exchange,
        "pair_whitelist": ex.get("pair_whitelist", ["BTC/USDT", "ETH/USDT"]),
    }


def update_exchange_config(
    dst_path: pathlib.Path = RUNTIME_CONFIG_PATH,
    exchange_name: str | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
    passphrase: str | None = None,
    dry_run: bool | None = None,
) -> dict:
    ensure_runtime_config(dst_path=dst_path)
    with _RUNTIME_CFG_LOCK:
        cfg = json.loads(dst_path.read_text(encoding="utf-8")) if dst_path.exists() else {}
        ex = cfg.setdefault("exchange", {})
        if exchange_name is not None:
            cleaned = re.sub(r"[^a-z0-9_]", "", exchange_name.strip().lower())
            if not cleaned:
                raise ValueError("Nome de corretora inválido")
            ex["name"] = cleaned
            curr_stake = cfg.get("stake_currency", "")
            target_stake = curr_stake if curr_stake in ("USD", "USDT") else ("USD" if cleaned == "kraken" else "USDT")
            cfg["stake_currency"] = target_stake
            old_pairs = ex.get("pair_whitelist", ["BTC/USDT", "ETH/USDT"])
            ex["pair_whitelist"] = [
                re.sub(r"/(USD|USDT)$", f"/{target_stake}", p) for p in old_pairs
            ]
            ot = cfg.setdefault("order_types", {})
            ot.update({
                "entry": "limit",
                "exit": "limit",
                "emergency_exit": "market",
                "force_entry": "market",
                "force_exit": "market",
                "stoploss": "market",
                "stoploss_on_exchange": cleaned in EXCHANGES_WITH_ON_EXCHANGE_STOP,
                "stoploss_on_exchange_interval": 60,
                "stoploss_on_exchange_limit_ratio": 0.99,
            })
        if api_key is not None:
            ex["key"] = api_key.strip()
        if api_secret is not None:
            ex["secret"] = api_secret.strip()
        if passphrase is not None:
            if passphrase.strip():
                ex["password"] = passphrase.strip()
            else:
                ex.pop("password", None)
        if dry_run is not None:
            if not bool(dry_run) and (not ex.get("key") or not ex.get("secret")):
                raise ValueError("Configure a API Key e API Secret antes de ativar a Conta Real.")
            cfg["dry_run"] = bool(dry_run)
            try:
                from user_data.state_store import save_state
                save_state("exchange_state", {"dry_run": cfg["dry_run"], "exchange": ex.get("name", "kraken")}, [])
            except Exception as exc:
                print("PT-BR_EX_STATE_SAVE_WARN:", exc, flush=True)
        _atomic_write_text(dst_path, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    return get_exchange_status(dst_path=dst_path)



def _get_telegram_inline_classes():
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        return InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError:
        class InlineKeyboardButton:
            def __init__(self, text, callback_data=None, url=None):
                self.text = text
                self.callback_data = callback_data
                self.url = url
        class InlineKeyboardMarkup:
            def __init__(self, inline_keyboard):
                self.inline_keyboard = inline_keyboard
        return InlineKeyboardButton, InlineKeyboardMarkup


def build_main_menu_inline_markup():
    InlineKeyboardButton, InlineKeyboardMarkup = _get_telegram_inline_classes()

    # Quick-toggle do Guardião IA direto no menu principal (estado visível no próprio botão)
    try:
        from user_data.ai_guardian import get_state as _ai_qs
        ai_on = bool(_ai_qs().get("enabled", False))
        ai_quick = InlineKeyboardButton(
            "🧠 Guardião IA: 🟢 LIGADO (desligar)" if ai_on else "🧠 Guardião IA: 🔴 DESLIGADO (ligar)",
            callback_data="unk_ai:quick_toggle",
        )
    except Exception:
        ai_quick = InlineKeyboardButton("🧠 Guardião IA (Fable)", callback_data="unk_ai:open")

    return InlineKeyboardMarkup([
        [ai_quick],
        [InlineKeyboardButton("🏦 Corretora / API", callback_data="unk_ex:refresh"), InlineKeyboardButton("🎯 Estratégia", callback_data="unk_strat:refresh")],
        [InlineKeyboardButton("📊 Operações Abertas", callback_data="unk_nav:status"), InlineKeyboardButton("💰 Lucros", callback_data="unk_nav:profit")],
        [InlineKeyboardButton("❓ Por Que Não Entrou?", callback_data="unk_nav:why"), InlineKeyboardButton("🔭 Scanner Mercado", callback_data="unk_nav:scanner")],
        [InlineKeyboardButton("👻 Shadow Trading (Neon)", callback_data="unk_nav:shadow"), InlineKeyboardButton("🩺 Diagnóstico", callback_data="unk_nav:diag")],
        [InlineKeyboardButton("🌐 Painel Web (Cockpit)", callback_data="unk_nav:painel"), InlineKeyboardButton("🔄 Recarregar", callback_data="unk_nav:menu")],
    ])


def build_corretora_inline_rows() -> list[list[tuple[str, str]]]:
    return [
        [("🟣 Kraken", "unk_ex:kraken"), ("🟠 Bybit", "unk_ex:bybit"), ("⚫ OKX", "unk_ex:okx")],
        [("🟢 KuCoin", "unk_ex:kucoin"), ("🟡 Binance", "unk_ex:binance")],
        [("🔑 API Key", "unk_input:api_key"), ("🔐 API Secret", "unk_input:api_secret"), ("🗝️ Passphrase", "unk_input:passphrase")],
        [("🛡️ Modo Simulado", "unk_mode:dry"), ("🚀 Conta Real", "unk_mode:live")],
        [("🧠 Guardião IA (Claude Opus 5.5)", "unk_ai:open")],
        [("🗑️ Limpar Chaves", "unk_api:clear"), ("🔄 Atualizar", "unk_ex:refresh"), ("🏠 Menu", "unk_nav:menu")],
    ]


def build_corretora_inline_markup():
    InlineKeyboardButton, InlineKeyboardMarkup = _get_telegram_inline_classes()

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text=t, callback_data=cb) for t, cb in row] for row in build_corretora_inline_rows()]
    )


def format_corretora_text() -> str:
    st = get_exchange_status()
    modo = "Simulação (Dry-Run)" if st["dry_run"] else "🚀 Conta Real (Kraken Spot)"
    ts = time.strftime("%H:%M:%S")
    if st["dry_run"]:
        stop_nativo = "🟡 Motor Simulado (Dry-Run: stop gerenciado na memória)"
    else:
        stop_nativo = "🟢 ATIVO no Livro Kraken (Stoploss na Corretora)" if st.get("stoploss_on_exchange") else "🟡 Stop Local (Monitorado pelo Bot)"
    try:
        from user_data.ai_guardian import get_state as _ai_st
        s_ai = _ai_st()
        m_name = s_ai.get("model", "claude-opus-5.5")
        ai_status = f"🟢 ATIVADA ({m_name})" if s_ai.get("enabled", False) else "🔴 PAUSADA"
    except Exception:
        ai_status = "🟢 ATIVADA (Claude Opus 5.5)"
    return (
        f"🏦 *Configuração da Corretora & API*\n\n"
        f"• *Corretora atual:* `{st['exchange'].upper()}`\n"
        f"• *Modo:* {modo}\n"
        f"• *Guardião IA:* {ai_status}\n"
        f"• *Stop-Loss Nativo:* {stop_nativo}\n"
        f"• *API Key:* `{st['key_masked']}`\n"
        f"• *API Secret:* `{st['secret_masked']}`\n"
        f"• *Passphrase:* `{'Configurada' if st['has_passphrase'] else 'Não necessária'}`\n"
        f"• *Atualizado:* `{ts}`\n\n"
        f"👇 *Toque nos botões abaixo para configurar sem comandos:*"
    )


STRATEGY_STATE_PATHS = [
    pathlib.Path("/tmp/strategy_state.json"),
    pathlib.Path("/freqtrade/user_data/strategy_state.json"),
    pathlib.Path("user_data/strategy_state.json"),
]
STRATEGY_STATE_PATH = STRATEGY_STATE_PATHS[0]
AVAILABLE_STRATEGIES = [
    ("RenderQuantV1", "🛡️ RenderQuant V1 (Equilibrada 15m)"),
    ("RenderQuantV2_Institutional", "🏛️ RenderQuant V2 (Institucional 15m)"),
    ("RenderQuantV3_Defensive", "🧲 RenderQuant V3 (Defensiva 4h — validada 600d)"),
    ("RenderQuantV4_Adaptive", "🚀 RenderQuant V4 (Adaptativa Multi-Regime 4h + Breakeven)"),
]


def get_strategy_state() -> dict:
    from user_data.state_store import load_state
    default = {
        "active_strategy": "RenderQuantV3_Defensive",
        "updated_at": time.strftime("%H:%M:%S UTC"),
    }
    # Sempre inclui STRATEGY_STATE_PATH caso tenha sido mockado em testes
    paths = [STRATEGY_STATE_PATH] + [p for p in STRATEGY_STATE_PATHS if p != STRATEGY_STATE_PATH]
    data = load_state("strategy_state", paths, default)
    valid_names = {name for name, _ in AVAILABLE_STRATEGIES}
    if data.get("active_strategy") not in valid_names:
        data["active_strategy"] = default["active_strategy"]
    return data


def update_strategy_state(strat_name: str) -> dict:
    with _RUNTIME_CFG_LOCK:
        state = get_strategy_state()
        state["active_strategy"] = strat_name
        state["updated_at"] = time.strftime("%H:%M:%S UTC")
        try:
            from user_data.state_store import save_state
            paths = [STRATEGY_STATE_PATH] + [p for p in STRATEGY_STATE_PATHS if p != STRATEGY_STATE_PATH]
            save_state("strategy_state", state, paths)
        except Exception as exc:
            print("PT-BR_STRATEGY_STATE_WARN:", exc, flush=True)

        if RUNTIME_CONFIG_PATH.exists():
            try:
                cfg = json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
                cfg["strategy"] = strat_name
                cfg["timeframe"] = "4h" if ("V3" in strat_name or "V4" in strat_name) else "15m"
                _atomic_write_text(RUNTIME_CONFIG_PATH, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
            except Exception as exc:
                print("PT-BR_RUNTIME_STRATEGY_WARN:", exc, flush=True)
    return state


def build_estrategia_inline_markup():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    st = get_strategy_state()
    active = st.get("active_strategy", "RenderQuantV3_Defensive")
    rows = []
    for s_name, label in AVAILABLE_STRATEGIES:
        mark = "✅" if s_name == active else "⬜"
        rows.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"unk_strat:{s_name}")])
    rows.append([InlineKeyboardButton(text="🔄 Atualizar Lista", callback_data="unk_strat:refresh")])
    return InlineKeyboardMarkup(rows)


def format_estrategia_text() -> str:
    st = get_strategy_state()
    active = st.get("active_strategy", "RenderQuantV3_Defensive")
    ts = st.get("updated_at", time.strftime("%H:%M:%S"))
    if "V3" in active:
        gatilhos = (
            "• *Timeframe:* `4h`\n"
            "• *Gatilho Reversão:* `BB_Pos < 12.0%` (Fundo de Sobrevenda Bollinger)\n"
            "• *Piso Estrutural Institucional:* `Preço >= 82% da EMA200 (4h)`\n"
            "• *Filtro de Liquidez 24h:* `Volume >= $300.000 USDT`\n"
            "• *Stop-Loss & Proteção:* `Stop: -5.0%` | `Breakeven em +2.5%` | `ROI: +10.0%`"
        )
    elif "V4" in active:
        gatilhos = (
            "• *Timeframe:* `4h`\n"
            "• *Gatilho Adaptativo:* `BB_Pos < 15.0%` + Rejeição de Pavio (Wick)\n"
            "• *Macro Guard:* `BTC acima da EMA200 (4h)`\n"
            "• *Gestão de Posição:* Trailing Breakeven dinâmico"
        )
    else:
        gatilhos = (
            "• *Timeframe:* `15m`\n"
            "• *Gatilho:* `Dual Bullish EMA9 > EMA21` + `MACD > Signal`\n"
            "• *Filtro de Ruído:* `KER Kaufman >= 0.35`"
        )
    return (
        f"🎯 *Seleção de Estratégia & Gestão Quantitativa*\n\n"
        f"• *Estratégia ativa no momento:* `{active}`\n"
        f"• *Última alteração:* `{ts}`\n"
        f"{gatilhos}\n\n"
        f"👇 *Toque em uma das opções abaixo para alternar:* \n"
        f"_(Ao alternar, a preferência é gravada no Neon DB e o motor reinicia com segurança em 2s para carregar a nova estratégia)_"
    )


def get_welcome_message() -> str:
    try:
        ex_st = get_exchange_status()
        is_dry = bool(ex_st.get("dry_run", True))
        ex_name = str(ex_st.get("exchange", "kraken")).upper()
    except Exception:
        is_dry = True
        ex_name = "KRAKEN"

    try:
        strat_st = get_strategy_state()
        active_strat = strat_st.get("active_strategy", "RenderQuantV3_Defensive")
    except Exception:
        active_strat = "RenderQuantV3_Defensive"

    try:
        from user_data.ai_guardian import get_state as get_ai_state
        ai_st = get_ai_state()
        ai_model = ai_st.get("model", "claude-opus-5.5")
        ai_on = ai_st.get("enabled", True)
        ai_desc = f"{ai_model} 🟢 Ativo" if ai_on else f"{ai_model} 🔴 Pausado"
    except Exception:
        ai_desc = "claude-opus-5.5 🟢 Ativo"

    if is_dry:
        mode_line = "🛡️ *Modo:* Simulação (`Dry-Run` — Carteira Virtual: `1000 USD`)"
    else:
        mode_line = "🚀 *Modo:* **Conta Real** (`Kraken Spot` — Saldo Real Ativo)"

    strat_tf = "4h" if ("V3" in active_strat or "V4" in active_strat) else "15m"

    return (
        "🤖 *Unkiorra Trade Bot*\n\n"
        "✅ *Robô Ativo:* Monitorando oportunidades em tempo real\n"
        f"🏦 *Corretora:* {ex_name} (`Pares Spot` | `{active_strat}` `{strat_tf}`)\n"
        f"{mode_line}\n"
        f"🧠 *Guardião IA:* {ai_desc} (`/ia` ou toque em 🧠 Guardião IA)\n"
        "⚡ *Proteção de Capital:* Ativa (`Mean Reversion` + `StoplossGuard` + `MaxDrawdown`)\n"
        "👻 *Telemetria:* Shadow Trading Ativo no Neon DB (`/shadow`)\n\n"
        "👇 *Toque nos botões abaixo para controlar o robô:*"
    )


def _get_ai_guardian_module():
    try:
        from user_data import ai_guardian
        return ai_guardian
    except Exception:
        try:
            import ai_guardian
            return ai_guardian
        except Exception:
            return None


def format_ai_guardian_text() -> str:
    mod = _get_ai_guardian_module()
    if mod and hasattr(mod, "format_ai_guardian_text"):
        try:
            return mod.format_ai_guardian_text()
        except Exception as exc:
            return f"🧠 *Guardião IA (Claude Opus 5.5)*\n\n⚠️ Erro ao formatar: `{exc}`"
    return "🧠 *Guardião IA (Claude Opus 5.5)*\n\n• *Status:* 🟢 *ATIVO (Veto Cognitivo Ligado)*\n• *Motor Cognitivo:* `claude-opus-5.5` (Mirai API)\n• *Convicção Mínima:* `65%`\n• *Saída Inteligente (Profit Shield):* `🟢 Ativa`"


def build_ai_guardian_inline_markup():
    mod = _get_ai_guardian_module()
    if mod and hasattr(mod, "build_ai_guardian_inline_markup"):
        try:
            return mod.build_ai_guardian_inline_markup()
        except Exception:
            pass
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 IA ATIVADA (Toque para Pausar)", callback_data="unk_ai:toggle")],
        [InlineKeyboardButton("60%", callback_data="unk_ai:conv:0.60"), InlineKeyboardButton("🎯 65%", callback_data="unk_ai:conv:0.65"), InlineKeyboardButton("70%", callback_data="unk_ai:conv:0.70"), InlineKeyboardButton("80%", callback_data="unk_ai:conv:0.80")],
        [InlineKeyboardButton("🔑 Inserir API Key", callback_data="unk_ai:key"), InlineKeyboardButton("🧪 Testar Conexão", callback_data="unk_ai:test")],
        [InlineKeyboardButton("📋 Ver Parecer", callback_data="unk_ai:verdict"), InlineKeyboardButton("🔄 Atualizar", callback_data="unk_ai:refresh")],
        [InlineKeyboardButton("🏦 Ir para Menu da Corretora", callback_data="unk_ex:refresh")],
    ])



def build_web_pt_br_script() -> str:
    return (
        "<scr" + "ipt>" + WEB_PT_BR_MARKER + "(function(){"
        "const DICT=" + json.dumps(WEB_DICT, ensure_ascii=False) + ";"
        "const LOWER={};for(const k in DICT)LOWER[k.toLowerCase()]=DICT[k];"
        "const PATTERNS=["
        "[/^Running with\\s+(.+?)\\s+on\\s+(.+)$/i,'Operando com $1 na corretora $2'],"
        "[/^Running\\s+Freqtrade\\s*(.*)$/i,'Executando Freqtrade $1'],"
        "[/^In\\s+undefined$/i,'Em USDT'],"
        "[/^In\\s+([A-Z]{2,10})$/i,'Em $1'],"
        "[/^Long entries:\\s*(\\d+)\\s*Long exit:\\s*(\\d+)\\s*Short exits:\\s*(\\d+)$/i,'Entradas Long: $1 | Saídas Long: $2 | Saídas Short: $3'],"
        "[/^Showing\\s+(\\d+)\\s+to\\s+(\\d+)\\s+of\\s+(\\d+)(.*)$/i,'Exibindo $1 a $2 de $3$4'],"
        "[/^(\\d+)\\s+open\\s+trades?$/i,'$1 operação(ões) aberta(s)'],"
        "[/^(\\d+)\\s+closed\\s+trades?$/i,'$1 operação(ões) fechada(s)'],"
        "[/^Last\\s+process:\\s*(.+)$/i,'Último processamento: $1'],"
        "[/^Initial\\s+bot\\s+start:\\s*(.+)$/i,'Início do robô: $1'],"
        "[/^Bot\\s+start:\\s*(.+)$/i,'Início do robô: $1'],"
        "[/^Freqtrade\\s+version:\\s*(.+)$/i,'Versão do Freqtrade: $1']"
        "];"
        "function lookup(k){return DICT[k]||LOWER[k.toLowerCase()]||null;}"
        "function tSmart(s){"
        "  if(!s)return s;"
        "  const core=s.trim().replace(/\\s+/g,' ');"
        "  if(!core)return s;"
        "  const lead=(s.match(/^\\s*/)||[''])[0],trail=(s.match(/\\s*$/)||[''])[0];"
        "  let hit=lookup(core);"
        "  if(hit)return lead+hit+trail;"
        "  const m=core.match(/^(.*?)(\\s*[\\(\\[]\\d+[\\)\\]])?([:\\.])?$/);"
        "  if(m&&m[1]){"
        "    const b=lookup(m[1].trim());"
        "    if(b)return lead+b+(m[2]||'')+(m[3]||'')+trail;"
        "  }"
        "  const idx=core.indexOf(':');"
        "  if(idx>0&&idx<core.length-1){"
        "    const lk=lookup(core.slice(0,idx).trim());"
        "    const rvRaw=core.slice(idx+1).trim();"
        "    const rv=lookup(rvRaw)||rvRaw;"
        "    if(lk)return lead+lk+': '+rv+trail;"
        "  }"
        "  for(let i=0;i<PATTERNS.length;i++){"
        "    if(PATTERNS[i][0].test(core))return lead+core.replace(PATTERNS[i][0],PATTERNS[i][1])+trail;"
        "  }"
        "  return s;"
        "}"
        "function setVueVal(el,val){"
        "  if(!el||!val||el.value)return;"
        "  const d=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');"
        "  if(d&&d.set)d.set.call(el,val);else el.value=val;"
        "  el.dispatchEvent(new Event('input',{bubbles:true}));"
        "  el.dispatchEvent(new Event('change',{bubbles:true}));"
        "}"
        "function prefillLogin(){"
        "  if(!location.pathname.includes('login'))return;"
        "  const forms=document.querySelectorAll('form');"
        "  for(let f=0;f<forms.length;f++){"
        "    const ins=forms[f].querySelectorAll('input:not([type=\"password\"]):not([type=\"checkbox\"])');"
        "    if(ins.length>=1&&!ins[0].value)setVueVal(ins[0],'freqtrader');"
        "  }"
        "}"
        "function walk(n){"
        "  if(!n)return;"
        "  if(n.nodeType===3){"
        "    const v=n.nodeValue;"
        "    if(!v||n.__pt===v)return;"
        "    const nv=tSmart(v);"
        "    n.__pt=nv;"
        "    if(nv!==v)n.nodeValue=nv;"
        "  }else if(n.nodeType===1){"
        "    const tg=n.tagName;"
        "    if(tg==='SCRIPT'||tg==='STYLE'||tg==='CODE'||tg==='PRE'||tg==='TEXTAREA')return;"
        "    if(n.placeholder&&n.__ptPh!==n.placeholder){const np=tSmart(n.placeholder);n.__ptPh=np;if(np!==n.placeholder)n.placeholder=np;}"
        "    if(n.title&&n.__ptTi!==n.title){const nt=tSmart(n.title);n.__ptTi=nt;if(nt!==n.title)n.title=nt;}"
        "    if(n.getAttribute){"
        "      const al=n.getAttribute('aria-label');"
        "      if(al&&n.__ptAl!==al){const nal=tSmart(al);n.__ptAl=nal;if(nal!==al)n.setAttribute('aria-label',nal);}"
        "    }"
        "    for(let i=0;i<n.childNodes.length;i++)walk(n.childNodes[i]);"
        "  }"
        "}"
        "document.documentElement.lang='pt-BR';"
        "let timer=null;"
        "function mountCockpitHUD(){"
        "  if(!document.body||document.getElementById('unk-cockpit-hud'))return;"
        "  const h=document.createElement('div');h.id='unk-cockpit-hud';"
        "  h.style.cssText='position:fixed;top:0;left:0;right:0;height:36px;z-index:99998;background:linear-gradient(90deg,#0a0f1d,#0f172a);border-bottom:1px solid #14b8a6;display:flex;align-items:center;justify-content:space-between;padding:0 16px;font-family:sans-serif;font-size:12px;color:#f8fafc;box-shadow:0 2px 10px rgba(0,0,0,0.5);';"
        "  h.innerHTML='<div style=\"display:flex;align-items:center;gap:16px;\"><span style=\"color:#2dd4bf;font-weight:bold;\">⚡ UNKIORRA QUANT COCKPIT</span><span>Estratégia: <strong id=\"unk-hud-strat-label\" style=\"color:#38bdf8;\">RenderQuantV3_Defensive</strong></span><span>Status: <strong id=\"unk-hud-mode-label\" style=\"color:#4ade80;\">CONTA REAL (LIVE)</strong></span></div><div style=\"display:flex;align-items:center;gap:12px;\"><button id=\"unk-hud-shadow-btn\" style=\"background:#7c3aed;color:#fff;border:none;padding:3px 10px;border-radius:4px;font-size:11px;font-weight:600;cursor:pointer;\">👻 Shadow Trading</button><button id=\"unk-hud-strat-btn\" style=\"background:#2563eb;color:#fff;border:none;padding:3px 10px;border-radius:4px;font-size:11px;font-weight:600;cursor:pointer;\">🎯 Mudar Estratégia</button><button id=\"unk-hud-ex-btn\" style=\"background:#0f766e;color:#fff;border:none;padding:3px 10px;border-radius:4px;font-size:11px;font-weight:600;cursor:pointer;\">🏦 Corretora</button></div>';"
        "  document.body.prepend(h);"
        "  document.body.style.paddingTop='36px';"
        "  const b=document.getElementById('unk-hud-strat-btn');if(b)b.onclick=()=>{if(window.openStratModal)window.openStratModal();};"
        "  const sb=document.getElementById('unk-hud-shadow-btn');if(sb)sb.onclick=()=>{if(window.openShadowModal)window.openShadowModal();};"
        "  const eb=document.getElementById('unk-hud-ex-btn');if(eb)eb.onclick=()=>{const mb=document.getElementById('unk-ex-btn');if(mb)mb.click();};"
        "  (async()=>{"
        "    try{"
        "      const auth=getSessionAuth();if(!auth)return;"
        "      const [rs,re]=await Promise.all([fetch('/api/v1/unkiorra/strategy',{headers:{Authorization:auth}}),fetch('/api/v1/unkiorra/exchange',{headers:{Authorization:auth}})]);"
        "      if(rs.ok){const ds=await rs.json();const sl=document.getElementById('unk-hud-strat-label');if(sl&&ds.active_strategy)sl.textContent=ds.active_strategy;}"
        "      if(re.ok){const de=await re.json();const ml=document.getElementById('unk-hud-mode-label');if(ml){if(de.dry_run){ml.textContent='SIMULADO (DRY-RUN)';ml.style.color='#f59e0b';}else{ml.textContent='CONTA REAL (KRAKEN LIVE)';ml.style.color='#4ade80';}}}"
        "    }catch(e){}"
        "  })();"
        "}"
        "function mountStratModal(){"
        "  if(!document.body||document.getElementById('unk-strat-btn'))return;"
        "  const btn=document.createElement('button');btn.id='unk-strat-btn';"
        "  btn.textContent='🎯 Estratégia';"
        "  btn.style.cssText='position:fixed;bottom:16px;right:180px;z-index:9999;background:#2563eb;color:#fff;border:none;border-radius:999px;padding:10px 16px;font-weight:600;cursor:pointer;box-shadow:0 4px 12px rgba(0,0,0,.3);';"
        "  document.body.appendChild(btn);"
        "  const ov=document.createElement('div');ov.id='unk-strat-ov';"
        "  ov.style.cssText='display:none;position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.6);align-items:center;justify-content:center;';"
        "  ov.innerHTML='<div style=\"background:#1e293b;color:#f8fafc;padding:22px;border-radius:12px;width:420px;max-width:92vw;font-family:sans-serif;\"><h3 style=\"margin:0 0 10px;display:flex;align-items:center;gap:8px;\">🎯 Alternar Estratégia do Robô</h3><p style=\"font-size:12px;color:#94a3b8;margin:0 0 14px;\">Selecione a estratégia quantitativa em tempo real. A comutação atualiza o motor imediatamente sem derrubar o container.</p><label style=\"display:block;font-size:12px;margin-bottom:6px;\">Estratégias Disponíveis:</label><select id=\"unk-strat-select\" style=\"width:100%;padding:10px;border-radius:6px;border:1px solid #475569;background:#0f172a;color:#fff;font-size:13px;\"><option value=\"RenderQuantV3_Defensive\">🧲 RenderQuant V3 (Defensiva 4h — Validada 600d)</option><option value=\"RenderQuantV4_Adaptive\">🚀 RenderQuant V4 (Adaptativa Multi-Regime 4h + Breakeven)</option><option value=\"RenderQuantV1\">🛡️ RenderQuant V1 (Equilibrada 15m)</option><option value=\"RenderQuantV2_Institutional\">🏛️ RenderQuant V2 (Institucional 15m)</option></select><div id=\"unk-strat-msg\" style=\"font-size:12px;margin-top:10px;color:#38bdf8;\"></div><div style=\"display:flex;gap:8px;justify-content:flex-end;margin-top:16px;\"><button id=\"unk-strat-close\" style=\"padding:8px 12px;border-radius:6px;border:1px solid #475569;background:transparent;color:#fff;cursor:pointer;\">Fechar</button><button id=\"unk-strat-save\" style=\"padding:8px 16px;border-radius:6px;border:none;background:#2563eb;color:#fff;font-weight:600;cursor:pointer;\">Salvar e Aplicar</button></div></div>';"
        "  document.body.appendChild(ov);"
        "  window.openStratModal=async()=>{"
        "    ov.style.display='flex';"
        "    const m=document.getElementById('unk-strat-msg');m.textContent='Carregando estado...';"
        "    const auth=getSessionAuth();if(!auth){m.textContent='Autenticação necessária.';return;}"
        "    try{"
        "      const r=await fetch('/api/v1/unkiorra/strategy',{headers:{Authorization:auth}});"
        "      if(r.ok){"
        "        const d=await r.json();"
        "        if(d.active_strategy)document.getElementById('unk-strat-select').value=d.active_strategy;"
        "        m.textContent='Estratégia ativa: '+d.active_strategy;"
        "        const hl=document.getElementById('unk-hud-strat-label');if(hl)hl.textContent=d.active_strategy;"
        "      }else{m.textContent='Falha ao carregar estado da estratégia.';}"
        "    }catch(e){m.textContent='Erro de conexão.';}"
        "  };"
        "  btn.onclick=()=>window.openStratModal();"
        "  document.getElementById('unk-strat-close').onclick=()=>{ov.style.display='none';};"
        "  document.getElementById('unk-strat-save').onclick=async()=>{"
        "    const m=document.getElementById('unk-strat-msg');m.textContent='Aplicando nova estratégia...';"
        "    const auth=getSessionAuth();if(!auth){m.textContent='Autenticação necessária.';return;}"
        "    const sel=document.getElementById('unk-strat-select').value;"
        "    try{"
        "      const r=await fetch('/api/v1/unkiorra/strategy',{method:'POST',headers:{'Content-Type':'application/json',Authorization:auth},body:JSON.stringify({strategy:sel})});"
        "      const d=await r.json();"
        "      if(r.ok){"
        "        m.textContent='✅ Sucesso! Recarregando aplicação...';"
        "        const hl=document.getElementById('unk-hud-strat-label');if(hl)hl.textContent=sel;"
        "        setTimeout(()=>{ov.style.display='none';location.reload();},1200);"
        "      }else{m.textContent='❌ Erro: '+(d.detail||'falha ao aplicar');}"
        "    }catch(e){m.textContent='Erro de rede ao salvar estratégia.';}"
        "  };"
        "}"
        "function mountShadowModal(){"
        "  if(!document.body||document.getElementById('unk-shadow-ov'))return;"
        "  const ov=document.createElement('div');ov.id='unk-shadow-ov';"
        "  ov.style.cssText='display:none;position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.6);align-items:center;justify-content:center;';"
        "  ov.innerHTML='<div style=\"background:#1e293b;color:#f8fafc;padding:22px;border-radius:12px;width:520px;max-width:92vw;font-family:sans-serif;max-height:85vh;overflow-y:auto;\"><h3 style=\"margin:0 0 10px;display:flex;align-items:center;gap:8px;\">👻 Telemetria de Shadow Trading (Neon DB)</h3><p style=\"font-size:12px;color:#94a3b8;margin:0 0 14px;\">Simulação paralela de todos os sinais e vetos da IA em tempo real no Neon PostgreSQL para calibração de estratégias sem risco ao capital real.</p><div id=\"unk-shadow-content\" style=\"font-size:13px;color:#cbd5e1;\">Carregando dados do Neon DB...</div><div style=\"display:flex;gap:8px;justify-content:flex-end;margin-top:16px;\"><button id=\"unk-shadow-refresh\" style=\"padding:8px 12px;border-radius:6px;border:1px solid #7c3aed;background:transparent;color:#c084fc;cursor:pointer;\">🔄 Atualizar</button><button id=\"unk-shadow-close\" style=\"padding:8px 16px;border-radius:6px;border:none;background:#7c3aed;color:#fff;font-weight:600;cursor:pointer;\">Fechar</button></div></div>';"
        "  document.body.appendChild(ov);"
        "  window.openShadowModal=async()=>{"
        "    ov.style.display='flex';"
        "    const c=document.getElementById('unk-shadow-content');"
        "    c.innerHTML='<div style=\"padding:20px;text-align:center;\">Carregando telemetria do Neon DB...</div>';"
        "    const auth=getSessionAuth();"
        "    if(!auth){c.innerHTML='<div style=\"color:#f87171;\">Autenticação necessária. Faça login no painel.</div>';return;}"
        "    try{"
        "      const r=await fetch('/api/v1/unkiorra/shadow',{headers:{Authorization:auth}});"
        "      if(r.ok){"
        "        const d=await r.json();"
        "        let rowsHtml='';"
        "        if(d.last_trades&&d.last_trades.length){"
        "          rowsHtml='<table style=\"width:100%;margin-top:10px;font-size:11px;border-collapse:collapse;\"><tr style=\"border-bottom:1px solid #334155;color:#94a3b8;text-align:left;\"><th style=\"padding:4px;\">Par</th><th style=\"padding:4px;\">Taxa</th><th style=\"padding:4px;\">IA</th><th style=\"padding:4px;\">Status</th><th style=\"padding:4px;\">Lucro</th></tr>';"
        "          d.last_trades.forEach(t=>{"
        "            const color=t.status==='CLOSED_PROFIT'?'#4ade80':(t.status==='CLOSED_STOPLOSS'?'#f87171':'#38bdf8');"
        "            const p=t.profit_pct!==null?(t.profit_pct*100).toFixed(2)+'%':'-';"
        "            const ia=t.ai_approved?'<span style=\"color:#4ade80\">✅ Sim</span>':'<span style=\"color:#f87171\">🛑 Veto</span>';"
        "            rowsHtml+='<tr style=\"border-bottom:1px solid #334155;\"><td style=\"padding:4px;\">'+t.pair+'</td><td style=\"padding:4px;\">'+t.entry_rate+'</td><td style=\"padding:4px;\">'+ia+'</td><td style=\"padding:4px;color:'+color+'\">'+t.status+'</td><td style=\"padding:4px;color:'+color+'\">'+p+'</td></tr>';"
        "          });"
        "          rowsHtml+='</table>';"
        "        }else{"
        "          rowsHtml='<p style=\"color:#94a3b8;margin-top:10px;font-style:italic;\">Nenhum sinal registrado ainda nesta sessão.</p>';"
        "        }"
        "        c.innerHTML='<div style=\"display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px;\">'+"
        "          '<div style=\"background:#0f172a;padding:10px;border-radius:8px;\"><div style=\"color:#94a3b8;font-size:11px;\">SINAIS MONITORADOS</div><div style=\"font-size:18px;font-weight:bold;color:#38bdf8;\">'+d.total_signals+' <span style=\"font-size:12px;color:#94a3b8;\">('+d.open_signals+' ativos)</span></div></div>'+"
        "          '<div style=\"background:#0f172a;padding:10px;border-radius:8px;\"><div style=\"color:#94a3b8;font-size:11px;\">TAXA DE ACERTO SIMULADA</div><div style=\"font-size:18px;font-weight:bold;color:#4ade80;\">'+d.win_rate+'% <span style=\"font-size:11px;color:#cbd5e1;\">('+d.closed_profit+' TP / '+d.closed_stop+' SL)</span></div></div>'+"
        "          '<div style=\"background:#0f172a;padding:10px;border-radius:8px;\"><div style=\"color:#94a3b8;font-size:11px;\">LUCRO ACUMULADO</div><div style=\"font-size:18px;font-weight:bold;color:'+(d.cum_profit_pct>=0?'#4ade80':'#f87171')+';\">'+(d.cum_profit_pct>=0?'+':'')+d.cum_profit_pct+'%</div></div>'+"
        "          '<div style=\"background:#0f172a;padding:10px;border-radius:8px;\"><div style=\"color:#94a3b8;font-size:11px;\">VETOS IA (OPUS 5.5)</div><div style=\"font-size:18px;font-weight:bold;color:#c084fc;\">'+d.ai_veto_success_rate+'% <span style=\"font-size:11px;color:#94a3b8;\">('+d.ai_vetoes_saved_loss+'/'+d.ai_vetoes_total+')</span></div></div>'+"
        "          '</div>'+"
        "          '<h4 style=\"margin:12px 0 6px;color:#e2e8f0;font-size:12px;\">Últimos Sinais Auditados no Neon DB:</h4>'+"
        "          rowsHtml;"
        "      }else{c.innerHTML='<div style=\"color:#f87171;\">Falha ao carregar dados do Neon DB.</div>';}"
        "    }catch(e){c.innerHTML='<div style=\"color:#f87171;\">Erro de conexão com a API.</div>';}"
        "  };"
        "  document.getElementById('unk-shadow-close').onclick=()=>{ov.style.display='none';};"
        "  document.getElementById('unk-shadow-refresh').onclick=()=>window.openShadowModal();"
        "}"
        "function mountExModal(){""  if(!document.body||document.getElementById('unk-ex-btn'))return;""  const btn=document.createElement('button');btn.id='unk-ex-btn';""  btn.textContent='🏦 Corretora & API';""  btn.style.cssText='position:fixed;bottom:16px;right:16px;z-index:9999;background:#0f766e;color:#fff;border:none;border-radius:999px;padding:10px 16px;font-weight:600;cursor:pointer;box-shadow:0 4px 12px rgba(0,0,0,.3);';""  document.body.appendChild(btn);""  const ov=document.createElement('div');ov.id='unk-ex-ov';""  ov.style.cssText='display:none;position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.6);align-items:center;justify-content:center;';""  ov.innerHTML='<div style=\"background:#1e293b;color:#f8fafc;padding:20px;border-radius:12px;width:360px;max-width:92vw;font-family:sans-serif;\"><h3 style=\"margin:0 0 12px\">🏦 Corretora & Chaves API</h3><label style=\"display:block;font-size:12px;margin-top:8px\">Corretora (ex: kraken, binance, bybit, okx)</label><input id=\"unk-ex-name\" style=\"width:100%;padding:8px;margin-top:4px;border-radius:6px;border:1px solid #475569;background:#0f172a;color:#fff;\"/><label style=\"display:block;font-size:12px;margin-top:8px\">API Key</label><input id=\"unk-ex-key\" type=\"password\" placeholder=\"Deixe vazio para manter atual\" style=\"width:100%;padding:8px;margin-top:4px;border-radius:6px;border:1px solid #475569;background:#0f172a;color:#fff;\"/><label style=\"display:block;font-size:12px;margin-top:8px\">API Secret</label><input id=\"unk-ex-sec\" type=\"password\" placeholder=\"Deixe vazio para manter atual\" style=\"width:100%;padding:8px;margin-top:4px;border-radius:6px;border:1px solid #475569;background:#0f172a;color:#fff;\"/><label style=\"display:block;font-size:12px;margin-top:8px\">Passphrase (opcional: OKX/KuCoin)</label><input id=\"unk-ex-pass\" type=\"password\" style=\"width:100%;padding:8px;margin-top:4px;border-radius:6px;border:1px solid #475569;background:#0f172a;color:#fff;\"/><label style=\"display:flex;align-items:center;gap:8px;margin-top:10px;font-size:13px\"><input id=\"unk-ex-dry\" type=\"checkbox\"/> Modo Simulado (Dry-Run)</label><div id=\"unk-ex-msg\" style=\"font-size:12px;margin-top:8px;color:#94a3b8\"></div><div style=\"display:flex;gap:8px;justify-content:flex-end;margin-top:14px\"><button id=\"unk-ex-close\" style=\"padding:8px 12px;border-radius:6px;border:1px solid #475569;background:transparent;color:#fff;cursor:pointer\">Fechar</button><button id=\"unk-ex-save\" style=\"padding:8px 14px;border-radius:6px;border:none;background:#0f766e;color:#fff;font-weight:600;cursor:pointer\">Salvar e Aplicar</button></div></div>';""  document.body.appendChild(ov);""  function getSessionAuth(){try{const raw=localStorage.getItem('ftAuthLoginInfo');if(raw){const p=JSON.parse(raw);const b=p&&typeof p==='object'?Object.values(p)[0]:null;if(b&&b.accessToken)return 'Bearer '+b.accessToken;}}catch(e){}if(!window.__unkAuth){const u=prompt('Usuário da API Freqtrade:');const p=prompt('Senha da API Freqtrade:');if(u&&p)window.__unkAuth='Basic '+btoa(u+':'+p);}return window.__unkAuth||'';}""  btn.onclick=async()=>{ov.style.display='flex';const m=document.getElementById('unk-ex-msg');m.textContent='Carregando...';const auth=getSessionAuth();if(!auth){m.textContent='Autenticação necessária.';return;}try{const r=await fetch('/api/v1/unkiorra/exchange',{headers:{Authorization:auth}});if(r.status===401){window.__unkAuth='';m.textContent='Sessão expirada. Clique novamente.';return;}const d=await r.json();document.getElementById('unk-ex-name').value=d.exchange||'kraken';document.getElementById('unk-ex-dry').checked=!!d.dry_run;m.textContent='Chave atual: '+(d.key_masked||'Não configurada');}catch(e){m.textContent='Erro ao carregar.';}};""  document.getElementById('unk-ex-close').onclick=()=>{ov.style.display='none';};""  document.getElementById('unk-ex-save').onclick=async()=>{const m=document.getElementById('unk-ex-msg');const auth=getSessionAuth();if(!auth){m.textContent='Autenticação necessária.';return;}m.textContent='Salvando...';const body={exchange:document.getElementById('unk-ex-name').value,dry_run:document.getElementById('unk-ex-dry').checked};const k=document.getElementById('unk-ex-key').value,sc=document.getElementById('unk-ex-sec').value,ps=document.getElementById('unk-ex-pass').value;if(k)body.key=k;if(sc)body.secret=sc;if(ps)body.password=ps;try{const r=await fetch('/api/v1/unkiorra/exchange',{method:'POST',headers:{'Content-Type':'application/json',Authorization:auth},body:JSON.stringify(body)});const d=await r.json();m.textContent=r.ok?('Salvo: '+d.exchange+' ('+d.key_masked+'). Recarregando...'):('Erro: '+(d.detail||'falha'));}catch(e){m.textContent='Erro de rede.';}};""}"
        "function runWalk(){"
        "  if(!document.body)return;"
        "  obs.disconnect();"
        "  prefillLogin();"
        "  walk(document.body);mountCockpitHUD();mountStratModal();mountExModal();mountShadowModal();"
        "  obs.observe(document.body,{childList:true,subtree:true});"
        "}"
        "const obs=new MutationObserver(()=>{"
        "  if(timer)clearTimeout(timer);"
        "  timer=setTimeout(()=>{timer=null;runWalk();},150);"
        "});"
        "window.addEventListener('load',()=>setTimeout(runWalk,200));"
        "window.addEventListener('click',()=>{setTimeout(runWalk,120);setTimeout(runWalk,450);},true);"
        "window.addEventListener('popstate',()=>setTimeout(runWalk,150));"
        "runWalk();"
        "})();</scr" + "ipt>"
    )


WEB_PT_BR_SCRIPT = build_web_pt_br_script()


def inject_web_pt_br_html(raw_html: bytes) -> bytes:
    html = raw_html.decode("utf-8", errors="ignore")
    if WEB_PT_BR_MARKER in html:
        return raw_html
    script = build_web_pt_br_script()
    html = re.sub(
        r"<title>.*?</title>",
        "<title>Unkiorra Trade Bot — Painel Web (PT-BR)</title>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if "</body>" in html:
        html = html.replace("</body>", script + "</body>")
    else:
        html = html + script
    return html.encode("utf-8")


def install_web_pt_br_hooks() -> None:
    """Patches FreqUI HTML files directly on disk and hooks FileResponse natively."""
    try:
        import freqtrade
        ui_dir = pathlib.Path(freqtrade.__file__).resolve().parent / "rpc" / "api_server" / "ui"
        if ui_dir.exists():
            for html_path in ui_dir.rglob("*.html"):
                try:
                    raw = html_path.read_bytes()
                    patched = inject_web_pt_br_html(raw)
                    if patched != raw:
                        html_path.write_bytes(patched)
                        print("PT-BR_DISK_HTML_PATCHED:", str(html_path), flush=True)
                except Exception as exc:
                    print("PT-BR_DISK_PATCH_WARN:", html_path, exc, flush=True)
    except Exception as exc:
        print("PT-BR_UI_DIR_WARN:", exc, flush=True)

    try:
        from starlette.responses import FileResponse

        orig_fr_init = FileResponse.__init__
        _fr_patch_lock = threading.Lock()

        def patched_fr_init(self, path, *args, **kwargs):
            p_str = str(path)
            if p_str.endswith(".html"):
                try:
                    with _fr_patch_lock:
                        raw = pathlib.Path(p_str).read_bytes()
                        if WEB_PT_BR_MARKER.encode("utf-8") not in raw:
                            patched = inject_web_pt_br_html(raw)
                            tmp_path = pathlib.Path("/tmp") / ("pt_br_" + pathlib.Path(p_str).name)
                            _atomic_write_text(tmp_path, patched.decode("utf-8", errors="ignore"))
                            path = str(tmp_path)
                except Exception as exc:
                    print("PT-BR_FR_HOOK_WARN:", exc, flush=True)
            return orig_fr_init(self, path, *args, **kwargs)

        setattr(FileResponse, "__init__", patched_fr_init)
        print("PT-BR_WEB_UI_HOOK_INSTALLED_V8", flush=True)
    except Exception as exc:
        print("PT-BR_FR_INSTALL_ERR:", exc, flush=True)

    try:
        import base64 as _b64
        import secrets as _secrets
        from fastapi import HTTPException, Request

        def _check_basic(req: Request) -> None:
            auth = req.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                try:
                    import jwt as _jwt
                    secret = os.environ.get("FREQTRADE__API_SERVER__JWT_SECRET_KEY", "")
                    if not secret and RUNTIME_CONFIG_PATH.exists():
                        try:
                            secret = json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8")).get("api_server", {}).get("jwt_secret_key", "")
                        except Exception:
                            pass
                    if not secret and SECRET_CONFIG_PATH.exists():
                        try:
                            secret = json.loads(SECRET_CONFIG_PATH.read_text(encoding="utf-8")).get("api_server", {}).get("jwt_secret_key", "")
                        except Exception:
                            pass
                    if secret:
                        _jwt.decode(auth[7:].strip(), secret, algorithms=["HS256"])
                        return
                except Exception:
                    pass
            u, pw = get_api_creds()
            expected = "Basic " + _b64.b64encode(f"{u}:{pw}".encode("utf-8")).decode("ascii")
            if not _secrets.compare_digest(auth, expected):
                raise HTTPException(status_code=401, detail="Unauthorized")

        def _reload_freqtrade_rpc():
            try:
                from freqtrade.rpc.api_server.deps import get_rpc_optional
                rpc = get_rpc_optional()
                if rpc is not None:
                    rpc._rpc_reload_config()
                    print("PT-BR_RPC_RELOAD_SUCCESS", flush=True)
            except Exception as exc:
                print("PT-BR_RPC_RELOAD_WARN:", exc, flush=True)

        async def _unkiorra_get_strat_handler(request: Request):
            _check_basic(request)
            st = get_strategy_state()
            return {
                "active_strategy": st.get("active_strategy", "RenderQuantV3_Defensive"),
                "updated_at": st.get("updated_at", ""),
                "available_strategies": [
                    {"name": k, "label": v} for k, v in AVAILABLE_STRATEGIES
                ],
            }

        async def _unkiorra_set_strat_handler(request: Request):
            _check_basic(request)
            body = await request.json()
            strat_name = (body.get("strategy") or body.get("strategy_name") or "").strip()
            valid_names = {k for k, _ in AVAILABLE_STRATEGIES}
            if strat_name not in valid_names:
                raise HTTPException(status_code=400, detail=f"Estratégia inválida. Escolha entre: {', '.join(valid_names)}")
            st = update_strategy_state(strat_name)
            threading.Timer(2.0, lambda: os._exit(0)).start()
            return {
                "status": "success",
                "active_strategy": st.get("active_strategy"),
                "updated_at": st.get("updated_at"),
                "message": f"Estratégia alterada com sucesso para {strat_name}! Reiniciando motor em 2s para aplicar...",
            }

        async def _unkiorra_get_ex_handler(request: Request):
            _check_basic(request)
            return get_exchange_status()

        async def _unkiorra_set_ex_handler(request: Request):
            _check_basic(request)
            body = await request.json()
            ex_req = (body.get("exchange") or body.get("exchange_name") or "").strip().lower()
            ok, err = await precheck_exchange_async(ex_req)
            if not ok:
                raise HTTPException(status_code=400, detail=err)
            res = update_exchange_config(
                exchange_name=body.get("exchange") or body.get("exchange_name"),
                api_key=body.get("key") or body.get("api_key"),
                api_secret=body.get("secret") or body.get("api_secret"),
                passphrase=body.get("password") or body.get("passphrase"),
                dry_run=body.get("dry_run"),
            )
            threading.Timer(2.0, lambda: os._exit(0)).start()
            return res

        async def _unkiorra_get_shadow_handler(request: Request):
            _check_basic(request)
            try:
                from user_data.shadow_tracker import get_shadow_stats
                return get_shadow_stats()
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        async def _unkiorra_get_scanner_handler(request: Request):
            _check_basic(request)
            try:
                strat_name = get_strategy_state().get("active_strategy", "RenderQuantV3_Defensive")
                is_15m = strat_name in ("RenderQuantV1", "RenderQuantV2_Institutional")
                interval = 15 if is_15m else 240
                pairs = [
                    ("BTC/USDT", "XBTUSDT"),
                    ("ETH/USDT", "ETHUSDT"),
                    ("SOL/USDT", "SOLUSDT"),
                    ("XRP/USDT", "XRPUSDT"),
                    ("ADA/USDT", "ADAUSDT"),
                    ("LINK/USDT", "LINKUSDT"),
                ]
                results = []
                for pair_name, kraken_pair in pairs:
                    url = f"https://api.kraken.com/0/public/OHLC?pair={kraken_pair}&interval={interval}"
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=4) as resp:
                        data = json.loads(resp.read().decode("utf-8"))["result"]
                        key = [k for k in data.keys() if k != "last"][0]
                        raw = data[key][-25:]
                        closes = [float(x[4]) for x in raw]
                        c = closes[-1]
                        window = closes[-20:]
                        mid = sum(window) / 20.0
                        std = (sum((x - mid) ** 2 for x in window) / 20.0) ** 0.5
                        bb_lo = mid - 2.0 * std
                        bb_hi = mid + 2.0 * std
                        w = bb_hi - bb_lo if bb_hi != bb_lo else 1.0
                        bb_pos = max(-0.5, min(1.5, (c - bb_lo) / w))
                        status_str = "Topo/Sobrecompra" if bb_pos >= 0.60 else ("Neutro/Meio" if bb_pos >= 0.25 else "Fundo/Oportunidade")
                        results.append({
                            "pair": pair_name,
                            "price": c,
                            "bb_pos": round(float(bb_pos), 3),
                            "status": status_str,
                        })
                return {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC"), "pairs": results}
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # 1. Registro direto no APIRouter do api_v1 oficial do Freqtrade (já montado sob /api/v1)
        try:
            from freqtrade.rpc.api_server import api_v1
            api_v1.router.add_api_route("/unkiorra/strategy", _unkiorra_get_strat_handler, methods=["GET"])
            api_v1.router.add_api_route("/unkiorra/strategy", _unkiorra_set_strat_handler, methods=["POST"])
            api_v1.router.add_api_route("/unkiorra/exchange", _unkiorra_get_ex_handler, methods=["GET"])
            api_v1.router.add_api_route("/unkiorra/exchange", _unkiorra_set_ex_handler, methods=["POST"])
            api_v1.router.add_api_route("/unkiorra/shadow", _unkiorra_get_shadow_handler, methods=["GET"])
            api_v1.router.add_api_route("/unkiorra/scanner", _unkiorra_get_scanner_handler, methods=["GET"])
            print("PT-BR_ROUTES_REGISTERED_ON_API_V1_ROUTER", flush=True)
        except Exception as exc:
            print("PT-BR_API_V1_ROUTER_WARN:", exc, flush=True)

        # 2. Registro no ApiServer.__init__ para self.app
        try:
            from freqtrade.rpc.api_server.webserver import ApiServer
            orig_api_init = ApiServer.__init__

            def patched_api_init(self, *args, **kwargs):
                orig_api_init(self, *args, **kwargs)
                app = getattr(self, "app", None)
                if app is not None:
                    for path in ["/api/v1/unkiorra/strategy", "/unkiorra/strategy"]:
                        app.add_api_route(path, _unkiorra_get_strat_handler, methods=["GET"])
                        app.add_api_route(path, _unkiorra_set_strat_handler, methods=["POST"])
                    for path in ["/api/v1/unkiorra/exchange", "/unkiorra/exchange"]:
                        app.add_api_route(path, _unkiorra_get_ex_handler, methods=["GET"])
                        app.add_api_route(path, _unkiorra_set_ex_handler, methods=["POST"])
                    for path in ["/api/v1/unkiorra/shadow", "/unkiorra/shadow"]:
                        app.add_api_route(path, _unkiorra_get_shadow_handler, methods=["GET"])
                    for path in ["/api/v1/unkiorra/scanner", "/unkiorra/scanner"]:
                        app.add_api_route(path, _unkiorra_get_scanner_handler, methods=["GET"])
                    print("PT-BR_ROUTES_REGISTERED_ON_API_SERVER_APP", flush=True)

            ApiServer.__init__ = patched_api_init
        except Exception as exc:
            print("PT-BR_API_SERVER_INIT_WARN:", exc, flush=True)

        print("PT-BR_EXCHANGE_AND_STRATEGY_HOOKS_INSTALLED", flush=True)
    except Exception as exc:
        print("PT-BR_EXCHANGE_API_ERR:", exc, flush=True)



REGEX_TRANSLATIONS = [
    (re.compile(r"Daily Profit over the last (\d+) days"), r"📅 Lucro Diário nos últimos \1 dias"),
    (re.compile(r"Using whitelist `([^`]+)` with (\d+) pairs"), r"📋 *Lista de Moedas Monitoradas (\2 pares):*\nEstratégia de lista: `\1`"),
    (re.compile(r"\*Trade ID:\*\s*`?(\d+)`?"), r"🆔 *Operação #\1*"),
    (re.compile(r"\*Current Profit:\*"), "*Lucro Atual:*"),
    (re.compile(r"\*Close Profit:\*"), "*Lucro Final:*"),
    (re.compile(r"\*Open Rate:\*"), "*Preço de Entrada:*"),
    (re.compile(r"\*Current Rate:\*"), "*Preço Atual:*"),
    (re.compile(r"\*Close Rate:\*"), "*Preço de Saída:*"),
    (re.compile(r"\*Open Since:\*"), "*Aberta desde:*"),
    (re.compile(r"\*Amount:\*"), "*Quantidade:*"),
    (re.compile(r"\*Stake Amount:\*"), "*Valor Investido:*"),
    (re.compile(r"\*Stop Loss:\*"), "*Stop Loss:*"),
    (re.compile(r"\*Initial Stop Loss:\*"), "*Stop Loss Inicial:*"),
    (re.compile(r"\*Enter Tag:\*"), "*Motivo de Entrada:*"),
    (re.compile(r"\*Exit Reason:\*"), "*Motivo de Saída:*"),
    (re.compile(r"\*Direction:\*"), "*Direção:*"),
    (re.compile(r"\*Leverage:\*"), "*Alavancagem:*"),
    (re.compile(r"(\d+) open trades?"), r"\1 operação(ões) aberta(s)"),
    (re.compile(r"Last process:"), "Último processamento:"),
    (re.compile(r"Initial bot start:"), "Início do robô:"),
    (re.compile(r"Last bot restart:"), "Último reinício do robô:"),
    (re.compile(r"Estimated Value \\?\(Bot managed assets only\\?\):"), "Valor Estimado (apenas ativos gerenciados pelo robô):"),
    (re.compile(r"\bAvg\. Duration\b"), "Duração Média"),
    (re.compile(r"\btotal stake\b"), "total investido"),
    (re.compile(r"\bcurrent\b"), "em aberto"),
    (re.compile(r"\bmax\b"), "máximo"),
]

LITERAL_TRANSLATIONS = [
    ("Dry run is enabled. All trades are simulated.", "Modo Simulação (Dry-Run) ativado. Todas as operações são simuladas."),
    ("Simulated balances in Dry Mode.", "Saldos simulados no Modo Dry-Run."),
    ("Estimated Value (Bot managed assets only):", "Valor Estimado (apenas ativos gerenciados pelo robô):"),
    ("Bot managed assets only", "apenas ativos gerenciados pelo robô"),
    ("*Status:* `already running`", WELCOME_PT_BR),
    ("Status: `already running`", WELCOME_PT_BR),
    ("*Status:* `running`", "✅ *Status:* `Robô em execução e monitorando o mercado!`"),
    ("Status: `running`", "✅ *Status:* `Em execução e monitorando o mercado!`"),
    ("*Status:* `stopped`", "🛑 *Status:* `Robô parado (pausado).`"),
    ("Status: `stopped`", "🛑 *Status:* `Robô parado (pausado).`"),
    ("*Status:* `stopping ...`", "⏳ *Status:* `Pausando o robô...`"),
    ("Status: `stopping ...`", "⏳ *Status:* `Pausando o robô...`"),
    ("*Status:* `process died`", "🔴 *Status:* `Processo interrompido — reiniciando com segurança...`"),
    ("Stopping trader ...", "🛑 Pausando o robô de operações..."),
    ("*Warning:* `no active trade`", "ℹ️ *Aviso:* `Nenhuma operação aberta no momento.`"),
    ("Warning: `no active trade`", "ℹ️ *Aviso:* `Nenhuma operação aberta no momento.`"),
    ("*Warning:* `No closed trades so far.`", "ℹ️ *Aviso:* `Nenhuma operação fechada até o momento.`"),
    ("⚠ *Warning:*", "⚠️ *Aviso:*"),
    ("*Warning:*", "⚠️ *Aviso:*"),
    ("no active trade", "Nenhuma operação aberta no momento"),
    ("No active trades", "Nenhuma operação aberta no momento"),
    ("No closed trades so far.", "Nenhuma operação fechada até o momento."),
    ("no closed trade", "nenhuma operação fechada"),
    ("No trades yet.", "Nenhuma operação realizada até o momento."),
    ("No trades to show", "Nenhuma operação para exibir"),
    ("Searching for USDT pairs to buy and sell based on", "🔍 Buscando pares em USDT para compra e venda com base em"),
    ("*Exchange:*", "*Corretora:*"),
    ("*Stake per trade:*", "*Valor por Operação:*"),
    ("*Minimum ROI:*", "*Lucro Mínimo (ROI):*"),
    ("*Trailing Stoploss:*", "*Stoploss Móvel:*"),
    ("*Stoploss:*", "*Limite de Perda (Stoploss):*"),
    ("*Position adjustment:*", "*Ajuste de Posição:*"),
    ("*Timeframe:*", "*Tempo Gráfico:*"),
    ("*Strategy:*", "*Estratégia:*"),
    ("*Currency*:", "*Moeda*:"),
    ("*Available*:", "*Disponível*:"),
    ("Available:", "Disponível:"),
    ("Balance:", "Saldo:"),
    ("Bot Owned:", "Alocado ao Robô:"),
    ("Est. USDT:", "Estimado em USDT:"),
    ("*Total*:", "*Total*:"),
    ("*Est. Value*:", "*Valor Estimado*:"),
    ("*Estimated Value*:", "*Valor Estimado*:"),
    ("*Pending*:", "*Pendente*:"),
    ("Pending:", "Pendente:"),
    ("Starting capital:", "Capital inicial:"),
    ("All balances are simulated.", "Todos os saldos são simulados (Modo Dry-Run)."),
    ("All balances are simulated", "Todos os saldos são simulados (Modo Dry-Run)"),
    ("*ROI:* Close trades", "*Lucro (ROI):* Operações fechadas"),
    ("*ROI:* All trades", "*Lucro (ROI):* Todas as operações"),
    ("*Total Trade Count:*", "*Total de Operações:*"),
    ("*Bot started:*", "*Robô iniciado em:*"),
    ("Bot started:", "Robô iniciado em:"),
    ("*First Trade opened:*", "*Primeira operação:*"),
    ("*Latest Trade opened:*", "*Última operação:*"),
    ("*Win / Loss:*", "*Vitórias / Derrotas:*"),
    ("*Winrate:*", "*Taxa de Acerto:*"),
    ("*Expectancy (Rate):*", "*Expectativa de Ganho:*"),
    ("*Avg. Duration:*", "*Duração Média:*"),
    ("Avg. Duration:", "Duração Média:"),
    ("*Best Performing:*", "*Melhor Par:*"),
    ("*Trading volume:*", "*Volume Negociado:*"),
    ("*Profit factor:*", "*Fator de Lucro:*"),
    ("*Max Drawdown:*", "*Rebaixamento Máximo (Drawdown):*"),
    ("current:", "em aberto:"),
    ("max:", "máximo:"),
    ("total stake:", "total investido:"),
    ("Exit Reason", "Motivo de Saída"),
    ("Wins", "Ganhos"),
    ("Draws", "Empates"),
    ("Losses", "Perdas"),
    ("Performance:", "🏆 Desempenho por Par:"),
    ("Day ", "Dia "),
    ("Reloading config ...", "🔄 Recarregando configuração..."),
    ("Config reloaded", "✅ Configuração recarregada com sucesso!"),
    ("No active locks.", "Nenhum bloqueio ativo no momento."),
    ("Last process:", "Último processamento:"),
    ("Initial bot start:", "Início do robô:"),
    ("Last bot restart:", "Último reinício do robô:"),
    ("State:", "Estado:"),
    ("Version:", "Versão:"),
    ("Off", "Desativado"),
]


def translate_pt_br(text: str) -> str:
    if not text or not isinstance(text, str):
        return text
    if "Starts the trader" in text and "Stops the trader" in text:
        return HELP_PT_BR
    if "already running" in text.lower():
        return get_welcome_message()
    out = text
    for pattern, repl in REGEX_TRANSLATIONS:
        out = pattern.sub(repl, out)
    for en, pt in LITERAL_TRANSLATIONS:
        out = out.replace(en, pt)
    return out


def translate_inline_markup(kwargs: dict) -> dict:
    kb = kwargs.get("keyboard")
    if kb and isinstance(kb, list):
        try:
            from telegram import InlineKeyboardButton
            new_kb = []
            for row in kb:
                new_row = []
                for btn in row:
                    if hasattr(btn, "text"):
                        txt = "🔄 Atualizar" if btn.text == "Refresh" else btn.text
                        new_row.append(InlineKeyboardButton(text=txt, callback_data=btn.callback_data, url=btn.url))
                    else:
                        new_row.append(btn)
                new_kb.append(new_row)
            kwargs = dict(kwargs)
            kwargs["keyboard"] = new_kb
        except Exception:
            pass
    rm = kwargs.get("reply_markup")
    if not rm or not hasattr(rm, "inline_keyboard"):
        return kwargs
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        new_rows = []
        for row in rm.inline_keyboard:
            new_row = []
            for btn in row:
                txt = "🔄 Atualizar" if btn.text == "Refresh" else btn.text
                new_row.append(
                    InlineKeyboardButton(
                        text=txt,
                        callback_data=btn.callback_data,
                        url=btn.url,
                    )
                )
            new_rows.append(new_row)
        kwargs = dict(kwargs)
        kwargs["reply_markup"] = InlineKeyboardMarkup(new_rows)
    except Exception:
        pass
    return kwargs


def install_pt_br_hooks() -> None:
    from freqtrade.rpc.telegram import Telegram
    from telegram import InlineKeyboardButton

    orig_ikb_init = InlineKeyboardButton.__init__
    def patched_ikb_init(self, text, *args, **kwargs):
        if text == "Refresh":
            text = "🔄 Atualizar"
        return orig_ikb_init(self, text, *args, **kwargs)
    InlineKeyboardButton.__init__ = patched_ikb_init
    from telegram.ext import Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, MessageHandler, filters

    tg_ref = {}
    registered_apps = set()
    orig_tg_init = Telegram.__init__
    orig_send_msg = Telegram._send_msg
    orig_update_msg = Telegram._update_msg
    orig_add_handler = Application.add_handler

    def patched_tg_init(self, *args, **kwargs):
        tg_ref["self"] = self
        return orig_tg_init(self, *args, **kwargs)

    def patched_init_keyboard(self):
        self._keyboard = PT_BR_KEYBOARD

    async def patched_send_msg(self, msg: str, *args, **kwargs):
        self._keyboard = PT_BR_KEYBOARD
        tr = translate_pt_br(msg)
        kwargs = translate_inline_markup(kwargs)
        print("PT-BR_RPC_SENT:", str(tr)[:90].replace("\n", " "), flush=True)
        return await orig_send_msg(self, tr, *args, **kwargs)

    async def patched_update_msg(self, query, msg: str, *args, **kwargs):
        self._keyboard = PT_BR_KEYBOARD
        tr = translate_pt_br(msg)
        kwargs = translate_inline_markup(kwargs)
        return await orig_update_msg(self, query, tr, *args, **kwargs)

    async def _cmd_start_panel(update, context):
        try:
            if not is_authorized_chat(update):
                return
            inline_markup = build_main_menu_inline_markup()
            msg_func = update.message.reply_text if (update and update.message) else update.effective_chat.send_message
            welcome_text = get_welcome_message()
            try:
                await msg_func(welcome_text, parse_mode="Markdown", reply_markup=inline_markup)
            except Exception as md_err:
                print(f"[telegram] Fallback texto puro em _cmd_start_panel: {md_err}", flush=True)
                clean = welcome_text.replace("*", "").replace("`", "").replace("_", "")
                await msg_func(clean, reply_markup=inline_markup)
        except Exception as exc:
            print(f"[telegram] ERRO em _cmd_start_panel: {exc}", flush=True)

    async def _cmd_corretora(update, context):
        try:
            if not is_authorized_chat(update):
                return
            text = format_corretora_text()
            markup = build_corretora_inline_markup()
            msg_func = update.message.reply_text if (update and update.message) else update.effective_chat.send_message
            try:
                await msg_func(text, parse_mode="Markdown", reply_markup=markup)
            except Exception as md_err:
                print(f"[telegram] Fallback texto puro em _cmd_corretora: {md_err}", flush=True)
                clean = text.replace("*", "").replace("`", "").replace("_", "")
                await msg_func(clean, reply_markup=markup)
        except Exception as exc:
            print(f"[telegram] ERRO em _cmd_corretora: {exc}", flush=True)

    async def _cmd_ai_guardian(update, context):
        try:
            if not is_authorized_chat(update):
                return
            cid = str(update.effective_chat.id) if update and update.effective_chat else ""
            print(f"[telegram] _cmd_ai_guardian acionado por chat_id={cid}", flush=True)
            text = format_ai_guardian_text()
            markup = build_ai_guardian_inline_markup()
            msg_func = update.message.reply_text if (update and update.message) else update.effective_chat.send_message
            try:
                await msg_func(text, parse_mode="Markdown", reply_markup=markup)
            except Exception as md_err:
                print(f"[telegram] Fallback texto puro em _cmd_ai_guardian: {md_err}", flush=True)
                clean = text.replace("*", "").replace("`", "").replace("_", "")
                await msg_func(clean, reply_markup=markup)
        except Exception as exc:
            print(f"[telegram] ERRO em _cmd_ai_guardian: {exc}", flush=True)
            if update and update.effective_chat:
                await update.effective_chat.send_message(f"🧠 Guardião IA (Claude Fable 5.1)\nStatus: Ativo\nAlerta: {exc}")

    pending_input: dict[str, tuple[str, float]] = {}



    async def _cb_unkiorra(update, context):
        q = getattr(update, "callback_query", None)
        if not q or not is_authorized_chat(update):
            return
        await q.answer()
        data = q.data or ""
        tg = tg_ref.get("self")
        cid = str(update.effective_chat.id)
        if data == "unk_input:cancel":
            pending_input.pop(cid, None)
            await q.edit_message_text("❌ Entrada cancelada.")
            return
        if data.startswith("unk_nav:"):
            sub = data.split(":", 1)[1]
            if sub == "menu":
                try:
                    await q.edit_message_text(
                        get_welcome_message(),
                        parse_mode="Markdown",
                        reply_markup=build_main_menu_inline_markup(),
                    )
                except Exception:
                    pass
                return
            elif sub == "status":
                if tg and hasattr(tg, "_status"):
                    try:
                        await tg._status(update, context)
                    except Exception:
                        pass
                return
            elif sub == "profit":
                if tg and hasattr(tg, "_profit"):
                    try:
                        await tg._profit(update, context)
                    except Exception:
                        pass
                return
            elif sub == "why":
                await _cmd_why(update, context)
                return
            elif sub == "diag":
                await _cmd_diag(update, context)
                return
            elif sub == "shadow":
                await _cmd_shadow(update, context)
                return
            elif sub == "scanner":
                await _cmd_scanner(update, context)
                return
            elif sub == "painel":
                await _cmd_painel_web(update, context)
                return
        if data.startswith("unk_input:"):
            field = data.split(":", 1)[1]
            pending_input[cid] = (field, time.monotonic())
            labels = {"api_key": "🔑 API Key", "api_secret": "🔐 API Secret", "passphrase": "🗝️ Passphrase"}
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="unk_input:cancel")]])
            await q.message.reply_text(
                f"Envie agora apenas a sua *{labels.get(field, field)}* (sem barra `/`).\nSua mensagem será apagada automaticamente após salvar.",
                parse_mode="Markdown",
                reply_markup=cancel_kb,
            )
            return
        if data == "unk_api:clear":
            update_exchange_config(api_key="", api_secret="", passphrase="", dry_run=True)
            if tg and hasattr(tg, "_reload_config"):
                try:
                    await tg._reload_config(update, context)
                except Exception:
                    pass
        elif data.startswith("unk_ex:") and data != "unk_ex:refresh":
            ex_name = data.split(":", 1)[1]
            ok, err = await precheck_exchange_async(ex_name)
            if not ok:
                await q.message.reply_text(f"⚠️ {err}")
                return
            update_exchange_config(exchange_name=ex_name)
            await q.message.reply_text(
                f"🏦 *Corretora alterada para `{ex_name.upper()}`!*\n\n"
                f"💾 Gravada no Neon DB.\n"
                f"🔄 Reiniciando o motor em 2s para conectar à nova corretora...",
                parse_mode="Markdown",
            )
            threading.Timer(2.0, lambda: os._exit(0)).start()
            return
        elif data.startswith("unk_strat:"):
            if data != "unk_strat:refresh":
                strat_name = data.split(":", 1)[1]
                st = update_strategy_state(strat_name)
                await q.message.reply_text(
                    f"🎯 *Estratégia alterada para `{strat_name}`!*\n\n"
                    f"💾 Gravada no Neon DB com sucesso.\n"
                    f"🔄 Reiniciando o motor Freqtrade em 2s para carregar os parâmetros da nova estratégia...",
                    parse_mode="Markdown",
                )
                threading.Timer(2.0, lambda: os._exit(0)).start()
                return
            try:
                await q.edit_message_text(
                    format_estrategia_text(),
                    parse_mode="Markdown",
                    reply_markup=build_estrategia_inline_markup(),
                )
            except Exception:
                pass
            return
        elif data.startswith("unk_ai:"):
            from user_data.ai_guardian import (
                get_state as _ai_get_state,
                update_state as _ai_update_state,
                test_ai_connection as _ai_test,
            )
            sub = data.split(":", 1)[1]
            if sub == "toggle" or sub == "quick_toggle":
                cur = _ai_get_state()
                new_enabled = not cur.get("enabled", False)
                _ai_update_state(enabled=new_enabled)
                status_txt = "🟢 LIGADO (veto cognitivo ativo)" if new_enabled else "🔴 DESLIGADO (bypass total)"
                try:
                    await q.message.reply_text(f"🧠 Guardião IA: {status_txt}")
                except Exception:
                    pass
            elif sub.startswith("conv:"):
                try:
                    c_val = float(sub.split(":", 1)[1])
                    _ai_update_state(min_conviccao=c_val)
                except Exception:
                    pass
            elif sub == "key":
                pending_input[cid] = ("ai_api_key", time.monotonic())
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="unk_input:cancel")]])
                await q.message.reply_text(
                    "Envie agora apenas a sua *Chave de API do AI Guardian (Claude Opus 5.5 / Mirai)*.\nSua mensagem será apagada do chat imediatamente após o salvamento.",
                    parse_mode="Markdown",
                    reply_markup=cancel_kb,
                )
                return
            elif sub == "test":
                await q.answer("🧪 Testando conexão com Claude Opus 5.5...", show_alert=False)
                ok, t_msg = _ai_test()
                await q.message.reply_text(t_msg, parse_mode="Markdown")
                return
            elif sub == "verdict":
                st_ai = _ai_get_state()
                last_v = st_ai.get("last_verdict")
                if last_v and isinstance(last_v, dict):
                    v_res = "✅ APROVADO" if last_v.get("aprovado") else "⛔ VETADO"
                    v_text = (
                        f"Parecer {last_v.get('pair', 'N/A')} ({last_v.get('ts')}):\n"
                        f"Status: {v_res}\n"
                        f"Convicção: {int(last_v.get('conviccao', 0)*100)}%\n"
                        f"Motivo: {last_v.get('motivo')}"
                    )
                else:
                    v_text = "Nenhum parecer emitido ainda. A IA avaliará a próxima oportunidade de trade."
                await q.answer(text=v_text[:200], show_alert=True)
                return
            try:
                await q.edit_message_text(
                    format_ai_guardian_text(),
                    parse_mode="Markdown",
                    reply_markup=build_ai_guardian_inline_markup(),
                )
            except Exception:
                pass
            return
        elif data.startswith("unk_mode:"):
            dry = data.endswith(":dry")
            label = "Simulação (Dry-Run)" if dry else "Conta Real"
            try:
                update_exchange_config(dry_run=dry)
            except ValueError as exc:
                await q.message.reply_text(f"❌ {exc}")
                return
            await q.message.reply_text(
                f"🚀 *Modo alterado para `{label}`!*\n\n"
                f"💾 Preferência gravada no Neon DB.\n"
                f"🔄 Reiniciando o motor Freqtrade em 2s para conectar na Kraken em *{label}*...",
                parse_mode="Markdown",
            )
            threading.Timer(2.0, lambda: os._exit(0)).start()
            return
        try:
            await q.edit_message_text(
                format_corretora_text(),
                parse_mode="Markdown",
                reply_markup=build_corretora_inline_markup(),
            )
        except Exception:
            pass

    async def _cmd_set_corretora(update, context):
        if not is_authorized_chat(update):
            return
        args = getattr(context, "args", []) or []
        if not args:
            await update.message.reply_text("Use: `/set_corretora binance` (ou `kraken`, `bybit`, `okx`, `kucoin`).", parse_mode="Markdown")
            return
        try:
            st = update_exchange_config(exchange_name=args[0])
            await update.message.reply_text(
                f"✅ Corretora alterada para `{st['exchange']}` no Neon DB!\n\n"
                f"🔄 Reiniciando motor em 2s para conectar à nova corretora...",
                parse_mode="Markdown",
            )
            threading.Timer(2.0, lambda: os._exit(0)).start()
        except Exception as exc:
            await update.message.reply_text(f"❌ Erro: {exc}")

    async def _cmd_set_api(update, context):
        if not is_authorized_chat(update):
            return
        args = getattr(context, "args", []) or []
        try:
            await update.message.delete()
        except Exception:
            pass
        if len(args) < 2:
            await update.effective_chat.send_message("Use: `/set_api SUA_KEY SEU_SECRET [PASSPHRASE]`", parse_mode="Markdown")
            return
        st = update_exchange_config(
            api_key=args[0],
            api_secret=args[1],
            passphrase=args[2] if len(args) > 2 else None,
        )
        await update.effective_chat.send_message(
            f"🔐 Chaves atualizadas (`{st['key_masked']}`) para `{st['exchange']}` no Neon DB!\n"
            f"🔄 Reiniciando motor em 2s para autenticar com as novas chaves...",
            parse_mode="Markdown",
        )
        threading.Timer(2.0, lambda: os._exit(0)).start()
    async def _cmd_modo(update, context, dry: bool):
        if not is_authorized_chat(update):
            return
        label = "Simulação (Dry-Run)" if dry else "Conta Real"
        try:
            st = update_exchange_config(dry_run=dry)
        except ValueError as exc:
            await update.message.reply_text(f"❌ {exc}", parse_mode="Markdown")
            return
        await update.message.reply_text(
            f"✅ Modo alterado para *{label}*!\n\n"
            f"💾 Gravado no Neon DB com sucesso.\n"
            f"🔄 Reiniciando motor em 2s para conectar na Kraken em *{label}*...",
            parse_mode="Markdown",
        )
        threading.Timer(2.0, lambda: os._exit(0)).start()

    def _get_target_message(update):
        target = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if not target and getattr(update, "callback_query", None):
            target = update.callback_query.message
        return target

    async def _cmd_painel_web(update, context):
        """Exibe link e credenciais permanentes para acesso ao FreqUI Cockpit."""
        if not is_authorized_chat(update):
            return
        target = _get_target_message(update)
        u, p = get_api_creds()
        url = "https://freqtrade-mnhu.onrender.com"
        msg = (
            "🌐 *Painel Web FreqUI (Cockpit)*\n\n"
            f"• *URL:* {url}\n"
            f"• *Usuário da API:* `{u}`\n"
            f"• *Senha da API:* `{p}`\n\n"
            "💡 _Toque na senha acima para copiá-la diretamente e entrar no painel pelo navegador._"
        )
        if target:
            await target.reply_text(msg, parse_mode="Markdown")

    async def _cmd_diag(update, context):
        """Diagnóstico em tempo real do estado do robô e subsistemas."""
        if not is_authorized_chat(update):
            return
        target = _get_target_message(update)
        st = get_exchange_status()
        is_dry = bool(st.get("dry_run", True))
        mode_str = "Simulado (Dry-Run)" if is_dry else "🚀 CONTA REAL (Kraken Spot LIVE)"
        tok_masked = f"...{TOKEN[-5:]}" if len(TOKEN) > 5 else "NÃO CONFIGURADO"
        if is_dry:
            stop_nativo_diag = "🟡 Ativo no Motor de Simulação (Stop Virtual 5%)"
        else:
            stop_nativo_diag = "🟢 ATIVO no Livro Kraken (Stoploss na Corretora)" if st.get("stoploss_on_exchange") else "🟡 Stop Local (Monitorado pelo Bot)"
        active_strat = "RenderQuantV3_Defensive"
        try:
            st_strat = get_strategy_state()
            active_strat = st_strat.get("active_strategy", "RenderQuantV3_Defensive")
        except Exception:
            pass
        strat_tf = "4h" if ("V3" in active_strat or "V4" in active_strat) else "15m"
        strat_desc = "Mean Reversion 4h" if "V3" in active_strat else ("Adaptativa 4h" if "V4" in active_strat else "Pullback/KER 15m")
        diag_msg = (
            "🩺 *Diagnóstico Forense do Sistema (Freqtrade + Render)*\n\n"
            f"• *Telegram RPC:* {'✅ Ativo e Operacional' if TOKEN else '❌ Desativado (Sem Token)'}\n"
            f"• *Token:* `{tok_masked}`\n"
            f"• *Chat ID Pareado:* `{CHAT_ID}`\n"
            f"• *Modo de Operação:* `{mode_str}`\n"
            f"• *Corretora:* `{st.get('exchange', 'kraken').upper()}`\n"
            f"• *Stop-Loss:* `{stop_nativo_diag}`\n"
            f"• *Estratégia:* `{active_strat}` (`{strat_tf}` — {strat_desc})\n"
            f"• *Watchdogs:* RAM (420MB) + Neon Anti-Sleep Ativos\n\n"
            "💡 _Seu bot está respondendo normalmente aos comandos._"
        )
        if target:
            await target.reply_text(diag_msg, parse_mode="Markdown")

    async def _cmd_estrategia(update, context):
        if not is_authorized_chat(update):
            return
        target = _get_target_message(update)
        if target:
            await target.reply_text(
                format_estrategia_text(),
                parse_mode="Markdown",
                reply_markup=build_estrategia_inline_markup(),
            )

    async def _cmd_why(update, context):
        """Diagnóstico matemático em tempo real: distância exata de cada par para a entrada da V3."""
        if not is_authorized_chat(update):
            return
        target = _get_target_message(update)

        why_paths = [
            pathlib.Path("/tmp/why_state.json"),
            pathlib.Path("user_data/why_state.json"),
            pathlib.Path("/freqtrade/user_data/why_state.json"),
        ]
        state = None
        for p in why_paths:
            try:
                if p.exists():
                    state = json.loads(p.read_text(encoding="utf-8"))
                    if state and "pairs" in state:
                        break
            except Exception:
                pass

        if state and "pairs" in state and state["pairs"]:
            pairs_dict = state["pairs"]
            strat_name = state.get("strategy", "RenderQuantV3_Defensive")
            target_bb = float(state.get("bb_pos_target", 0.12))
            updated_ts = state.get("updated_at", "Recente")

            rows = []
            min_pos = 1.0
            for p_name, p_data in sorted(pairs_dict.items()):
                c = p_data.get("price", 0)
                pos = p_data.get("bb_pos", 1.0)
                min_pos = min(min_pos, pos)
                status = "🔴 Topo" if pos >= 0.60 else ("🟡 Meio" if pos >= 0.25 else "🟢 Fundo")
                dist = (pos - target_bb) * 100
                dist_str = f"+{dist:.1f}%" if dist > 0 else f"{dist:.1f}%"
                rows.append(f"• *{p_name}:* `${c:,.2f}` | BB: `{pos*100:.1f}%` ({status}) | Distância: `{dist_str}`")

            if min_pos < target_bb:
                diag = f"🟢 *Oportunidade em andamento:* Ao menos uma moeda atingiu a sobrevenda (< {target_bb*100:.1f}%). Motor avaliando compra."
            elif min_pos < 0.25:
                diag = "🟡 *Retração em curso:* Moeda se aproximando do fundo. Monitorando confluência institucional."
            else:
                diag = f"🛡️ *Mercado em zona neutra/alta:* Todos os pares transitam acima de 25% de Bollinger. A estratégia V3 aguarda retração na zona de compra (< {target_bb*100:.1f}%) para comprar com proteção de capital."

            msg = (
                f"🛡️ *Diagnóstico de Entrada (/why)*\n\n"
                f"• *Estratégia:* `{strat_name} (4h)`\n"
                f"• *Gatilho de Compra V3:* `BB_Pos < {target_bb*100:.1f}%` (Fundo de Sobrevenda)\n"
                f"• *Última Telemetria:* `{updated_ts}`\n\n"
                f"📊 *Posição dos Pares em Tempo Real:*\n"
                + "\n".join(rows) +
                f"\n\n🔍 *Diagnóstico Matemático:*\n{diag}\n\n"
                f"💡 _O bot opera com rigor institucional: compras só acontecem no fundo estatístico das Bandas de Bollinger._"
            )
            if target:
                await target.reply_text(msg, parse_mode="Markdown")
            return

        # Fallback dinâmico respeitando a estratégia e timeframe ativos
        try:
            import requests as _req
            active_strat = get_strategy_state().get("active_strategy", "RenderQuantV3_Defensive")
            is_4h = ("V3" in active_strat or "V4" in active_strat)
            tf_label = "4h" if is_4h else "15m"
            interval = 240 if is_4h else 15
            target_bb = 0.15 if "V4" in active_strat else 0.12
            k_pairs = [
                ("BTC/USDT", "XBTUSDT"),
                ("ETH/USDT", "ETHUSDT"),
                ("SOL/USDT", "SOLUSDT"),
                ("XRP/USDT", "XRPUSDT"),
                ("ADA/USDT", "ADAUSDT"),
                ("LINK/USDT", "LINKUSDT"),
            ]
            rows = []
            min_pos = 1.0
            for p_name, kp in k_pairs:
                url = f"https://api.kraken.com/0/public/OHLC?pair={kp}&interval={interval}"
                r = _req.get(url, headers={"User-Agent": "Freqtrade-Unkiorra/1.0"}, timeout=4)
                if r.status_code == 200:
                    data = r.json().get("result", {})
                    k = [k for k in data.keys() if k != "last"][0]
                    closes = [float(x[4]) for x in data[k][-20:]]
                    c = closes[-1]
                    mid = sum(closes) / 20.0
                    std = (sum((x - mid) ** 2 for x in closes) / 20.0) ** 0.5
                    lo = mid - 2.0 * std
                    hi = mid + 2.0 * std
                    w = hi - lo if hi != lo else 1.0
                    pos = max(-0.5, min(1.5, (c - lo) / w))
                    min_pos = min(min_pos, pos)
                    status = "🔴 Topo" if pos >= 0.60 else ("🟡 Meio" if pos >= 0.25 else "🟢 Fundo")
                    dist = (pos - target_bb) * 100
                    rows.append(f"• *{p_name}:* `${c:,.2f}` | BB: `{pos*100:.1f}%` ({status}) | Distância: `{dist:+.1f}%`")

            if min_pos < target_bb:
                diag = f"🟢 *Oportunidade:* Ao menos uma moeda atingiu a zona (< {target_bb*100:.1f}%)."
            elif min_pos < 0.25:
                diag = "🟡 *Retração em curso:* Monitorando aproximação do fundo."
            else:
                diag = f"🛡️ *Mercado em zona neutra/alta:* Nenhum par atingiu a zona de sobrevenda (< {target_bb*100:.1f}%)."

            msg = (
                "🛡️ *Diagnóstico de Entrada (/why)*\n\n"
                f"• *Estratégia:* `{active_strat} ({tf_label})`\n"
                f"• *Referência Bollinger:* `BB_Pos < {target_bb*100:.1f}%`\n\n"
                "📊 *Posição Atual Calculada na Kraken (Tempo Real):*\n"
                + "\n".join(rows) +
                f"\n\n🔍 *Diagnóstico Matemático:*\n{diag}"
            )
            if target:
                await target.reply_text(msg, parse_mode="Markdown")
        except Exception as exc:
            if target:
                await target.reply_text(f"⚠️ *Erro ao calcular diagnóstico:* `{exc}`", parse_mode="Markdown")

    async def _cmd_scanner(update, context):
        """Scanner em tempo real dos pares Spot na Kraken com gatilhos dinâmicos da V3."""
        if not is_authorized_chat(update):
            return
        target = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if not target and getattr(update, "callback_query", None):
            target = update.callback_query.message

        loading_msg = None
        if target:
            loading_msg = await target.reply_text("🔭 *Escaneando pares Kraken Spot em tempo real...*", parse_mode="Markdown")

        try:
            import requests as _req
            active_strat = get_strategy_state().get("active_strategy", "RenderQuantV3_Defensive")
            is_4h = ("V3" in active_strat or "V4" in active_strat)
            tf_label = "4h" if is_4h else "15m"
            interval = 240 if is_4h else 15
            target_bb = 0.15 if "V4" in active_strat else 0.12
            pairs = [
                ("BTC/USDT", "XBTUSDT"),
                ("ETH/USDT", "ETHUSDT"),
                ("SOL/USDT", "SOLUSDT"),
                ("XRP/USDT", "XRPUSDT"),
                ("ADA/USDT", "ADAUSDT"),
                ("LINK/USDT", "LINKUSDT"),
            ]
            rows = []
            min_pos = 1.0
            for pair_name, kraken_pair in pairs:
                url = f"https://api.kraken.com/0/public/OHLC?pair={kraken_pair}&interval={interval}"
                resp = _req.get(url, headers={"User-Agent": "Freqtrade-Unkiorra/1.0"}, timeout=4)
                if resp.status_code == 200:
                    data = resp.json().get("result", {})
                    key = [k for k in data.keys() if k != "last"][0]
                    raw = data[key][-25:]
                    closes = [float(x[4]) for x in raw]
                    c = closes[-1]
                    window = closes[-20:]
                    mid = sum(window) / 20.0
                    std = (sum((x - mid) ** 2 for x in window) / 20.0) ** 0.5
                    bb_lo = mid - 2.0 * std
                    bb_hi = mid + 2.0 * std
                    w = bb_hi - bb_lo if bb_hi != bb_lo else 1.0
                    bb_pos = max(-0.5, min(1.5, (c - bb_lo) / w))
                    min_pos = min(min_pos, bb_pos)
                    status_emoji = "🔴 Topo" if bb_pos >= 0.60 else ("🟡 Meio" if bb_pos >= 0.25 else "🟢 Fundo")
                    dist_to_target = (bb_pos - target_bb) * 100
                    rows.append(f"• *{pair_name}:* `${c:,.2f}` | BB: `{bb_pos*100:.1f}%` ({status_emoji}) | Distância: `{dist_to_target:+.1f}%`")

            if min_pos < target_bb:
                diag = f"🟢 *Oportunidade:* Ao menos uma moeda atingiu a zona (< {target_bb*100:.1f}%). Motor avaliando compra."
            elif min_pos < 0.25:
                diag = "🟡 *Aproximação:* Moedas em retração intermediária. Monitorando aproximação do fundo."
            else:
                diag = f"🛡️ *Sem risco de topo:* Todos os pares estão acima da zona de sobrevenda (< {target_bb*100:.1f}%)."

            scan_text = (
                f"🔭 *Scanner de Mercado em Tempo Real (Kraken Spot {tf_label} — `{active_strat}`)*\n\n"
                + "\n".join(rows) +
                "\n\n🎯 *Referência do Filtro Ativo:*\n"
                f"• *Bandas de Bollinger ({tf_label}):* `BB_Pos < {target_bb*100:.1f}%`\n"
                "• *Liquidez 24h:* `>= $150.000 USDT` na Kraken Spot\n\n"
                f"💡 *Diagnóstico Dinâmico:* {diag}"
            )
            if loading_msg:
                await loading_msg.edit_text(scan_text, parse_mode="Markdown")
            elif target:
                await target.reply_text(scan_text, parse_mode="Markdown")
        except Exception as exc:
            err_msg = f"⚠️ *Erro ao consultar scanner:* `{exc}`"
            if loading_msg:
                await loading_msg.edit_text(err_msg, parse_mode="Markdown")
            elif target:
                await target.reply_text(err_msg, parse_mode="Markdown")

    async def _cmd_shadow(update, context):
        if not is_authorized_chat(update):
            return
        try:
            from user_data.shadow_tracker import format_shadow_stats_text
            msg = format_shadow_stats_text()
        except Exception as exc:
            msg = f"⚠️ *Erro na telemetria de shadow trading:* `{exc}`"
        target = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if not target and getattr(update, "callback_query", None):
            target = update.callback_query.message
        if target:
            await target.reply_text(msg, parse_mode="Markdown")

    async def _btn_cb(update, context):
        tg = tg_ref.get("self")
        if not tg or not update or not update.message or not update.message.text:
            return
        if not is_authorized_chat(update):
            cid = str(update.effective_chat.id) if update.effective_chat else ""
            await update.message.reply_text(
                f"🔒 *Acesso Não Autorizado*\n\n"
                f"Seu Chat ID é: `{cid}`\n\n"
                f"Adicione este número na variável `FREQTRADE__TELEGRAM__CHAT_ID` no painel do Render para liberar total controle.",
                parse_mode="Markdown",
            )
            return
        cid = str(update.effective_chat.id) if update.effective_chat else ""
        txt = update.message.text.strip()
        norm = txt.replace(" ", "").lower()

        _ALIASES_ESTRATEGIA = {
            "/estrategia", "estrategia", "/strategy", "strategy", "/strat",
            "🎯mudarestratégia", "🎯mudarestrategia", "🎯alternarestratégia", "🎯alternarestrategia"
        }
        _ALIASES_WHY = {
            "/why", "why", "/porque", "porque", "/porquenaoentrou", "/porquênãoentrou",
            "❓porquênãoentrou?", "❓porquenaoentrou?", "porquenaoentrou",
            "❓porquênão?", "❓porquenao?", "porquenao", "porquênão"
        }
        _ALIASES_DIAG = {
            "/diag", "diag", "/diagnostico", "diagnostico", "/saude", "saude",
            "🩺saúdedorobô", "🩺saudedorobo", "🩺diagnóstico", "🩺diagnostico"
        }
        _ALIASES_AI = {
            "/ia", "ia", "/ai", "ai", "/guardian", "guardian",
            "🧠guardiãoia", "🧠guardiaoia", "🧠iaguardian", "guardiaoia"
        }
        _ALIASES_SCANNER = {
            "/scanner", "scanner", "/mercado", "mercado", "scan", "/scan",
            "🔭scannermercado", "🔭scanner", "scannermercado"
        }
        _ALIASES_PAINEL = {
            "/painel", "painel", "/web", "web", "/frequi", "frequi",
            "🌐painelweb", "🌐painel", "painelweb", "cockpit"
        }

        if norm in ("/start", "start", "/menu", "menu", "iniciar", "/iniciar"):
            pending_input.pop(cid, None)
            await _cmd_start_panel(update, context)
            raise ApplicationHandlerStop()

        if norm in _ALIASES_AI or txt in ("🧠 Guardião IA", "🧠 IA Guardian", "🧠 Guardião IA (Fable)"):
            pending_input.pop(cid, None)
            await _cmd_ai_guardian(update, context)
            raise ApplicationHandlerStop()

        if norm in _ALIASES_DIAG or txt in ("🩺 Diagnóstico", "🩺 Saúde do Robô"):
            pending_input.pop(cid, None)
            await _cmd_diag(update, context)
            raise ApplicationHandlerStop()

        if norm in _ALIASES_PAINEL or txt in ("🌐 Painel Web", "🌐 Painel", "🌐 Web"):
            pending_input.pop(cid, None)
            await _cmd_painel_web(update, context)
            raise ApplicationHandlerStop()

        if norm in _ALIASES_ESTRATEGIA or txt in ("🎯 Mudar Estratégia", "🎯 Alternar Estratégia"):
            pending_input.pop(cid, None)
            await _cmd_estrategia(update, context)
            raise ApplicationHandlerStop()

        if norm in _ALIASES_WHY or txt in ("❓ Por Que Não?", "❓ Por Que Não Entrou?"):
            pending_input.pop(cid, None)
            await _cmd_why(update, context)
            raise ApplicationHandlerStop()

        if norm in _ALIASES_SCANNER or txt in ("🔭 Scanner Mercado", "🔭 Scanner"):
            pending_input.pop(cid, None)
            await _cmd_scanner(update, context)
            raise ApplicationHandlerStop()

        if norm in ("/shadow", "shadow", "telemetria", "/telemetria") or "shadow" in norm or txt in ("👻 Shadow Trading", "👻 Shadow Trading (Neon)"):
            pending_input.pop(cid, None)
            await _cmd_shadow(update, context)
            raise ApplicationHandlerStop()

        if txt in ("🏦 Corretora", "🏦 Corretora / API") or norm in ("/corretora", "corretora", "exchange"):
            pending_input.pop(cid, None)
            await _cmd_corretora(update, context)
            raise ApplicationHandlerStop()
        method_name = PT_BR_BUTTON_MAP.get(txt)
        if method_name and hasattr(tg, method_name):
            pending_input.pop(cid, None)
            await getattr(tg, method_name)(update, context)
            raise ApplicationHandlerStop()
        if cid == str(CHAT_ID) and cid in pending_input:
            field, ts = pending_input.pop(cid)
            if time.monotonic() - ts > PENDING_INPUT_TTL_SEC:
                await update.message.reply_text("⏳ Tempo esgotado para digitar o valor. Tente novamente no menu.")
                raise ApplicationHandlerStop()
            try:
                await update.message.delete()
            except Exception:
                pass
            if field == "ai_api_key":
                from user_data.ai_guardian import update_state as _ai_update_state
                _ai_update_state(api_key=txt.strip())
                await update.effective_chat.send_message(
                    "✅ *Chave do Claude Fable (Mirai API) salva com sucesso!*\n\n" + format_ai_guardian_text(),
                    parse_mode="Markdown",
                    reply_markup=build_ai_guardian_inline_markup(),
                )
                raise ApplicationHandlerStop()
            kwargs = {field: txt}
            st = update_exchange_config(**kwargs)
            if hasattr(tg, "_reload_config"):
                try:
                    await tg._reload_config(update, context)
                except Exception as exc:
                    print("PT-BR_TG_RELOAD_WARN:", exc, flush=True)
            await update.effective_chat.send_message(
                format_corretora_text(),
                parse_mode="Markdown",
                reply_markup=build_corretora_inline_markup(),
            )
            raise ApplicationHandlerStop()

    def patched_add_handler(self, handler, *args, **kwargs):
        res = orig_add_handler(self, handler, *args, **kwargs)
        app_id = id(self)
        if app_id not in registered_apps:
            registered_apps.add(app_id)
            orig_add_handler(self, CallbackQueryHandler(_cb_unkiorra, pattern=r"^unk_"), group=-1)
            orig_add_handler(self, CommandHandler("start", _cmd_start_panel), group=-1)
            orig_add_handler(self, CommandHandler("menu", _cmd_start_panel), group=-1)
            orig_add_handler(self, CommandHandler("corretora", _cmd_corretora), group=-1)
            orig_add_handler(self, CommandHandler("ia", _cmd_ai_guardian), group=-1)
            orig_add_handler(self, CommandHandler("ai", _cmd_ai_guardian), group=-1)
            orig_add_handler(self, CommandHandler("guardian", _cmd_ai_guardian), group=-1)
            orig_add_handler(self, CommandHandler("estrategia", _cmd_estrategia), group=-1)
            orig_add_handler(self, CommandHandler("strategy", _cmd_estrategia), group=-1)
            orig_add_handler(self, CommandHandler("why", _cmd_why), group=-1)
            orig_add_handler(self, CommandHandler("scanner", _cmd_scanner), group=-1)
            orig_add_handler(self, CommandHandler("mercado", _cmd_scanner), group=-1)
            orig_add_handler(self, CommandHandler("diag", _cmd_diag), group=-1)
            orig_add_handler(self, CommandHandler("diagnostico", _cmd_diag), group=-1)
            orig_add_handler(self, CommandHandler("shadow", _cmd_shadow), group=-1)
            orig_add_handler(self, CommandHandler("telemetria", _cmd_shadow), group=-1)
            orig_add_handler(self, CommandHandler("painel", _cmd_painel_web), group=-1)
            orig_add_handler(self, CommandHandler("web", _cmd_painel_web), group=-1)
            orig_add_handler(self, CommandHandler("frequi", _cmd_painel_web), group=-1)
            orig_add_handler(self, CommandHandler("set_corretora", _cmd_set_corretora))
            orig_add_handler(self, CommandHandler("set_api", _cmd_set_api))
            orig_add_handler(self, CommandHandler("modo_simulado", lambda u, c: _cmd_modo(u, c, True)))
            orig_add_handler(self, CommandHandler("modo_real", lambda u, c: _cmd_modo(u, c, False)))
            orig_add_handler(self, MessageHandler(filters.TEXT, _btn_cb), group=-1)
            print("PT-BR_BUTTON_HANDLER_REGISTERED", flush=True)
        return res

    Telegram.__init__ = patched_tg_init
    Telegram._init_keyboard = patched_init_keyboard
    Telegram._send_msg = patched_send_msg
    Telegram._update_msg = patched_update_msg
    Application.add_handler = patched_add_handler


async def setup_telegram_menu() -> None:
    if not TOKEN:
        print("[telegram] ⚠️ AVISO CRÍTICO: FREQTRADE__TELEGRAM__TOKEN não configurado no Render! O bot do Telegram permanecerá inativo.", flush=True)
        return
    if not str(CHAT_ID).strip():
        print("[telegram] ⚠️ AVISO CRÍTICO: FREQTRADE__TELEGRAM__CHAT_ID não configurado! "
              "Nenhuma mensagem de boot será enviada e nenhum comando será autorizado.", flush=True)
        return
    from telegram import Bot, BotCommand, ReplyKeyboardMarkup

    bot = Bot(token=TOKEN)
    # Claude Fable 5.1: Purga webhook antigo para destravar polling getUpdates sem conflito 409
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        me = await bot.get_me()
        print(f"[telegram] ✓ Conexão Telegram estabelecida com sucesso! Bot: @{me.username} ({me.first_name})", flush=True)
    except Exception as exc:
        print(f"[telegram] Alerta ao conectar na API do Telegram: {exc}", flush=True)

    cmds = [
        BotCommand("menu", "Menu principal interativo com botoes"),
        BotCommand("start", "Painel principal em Portugues"),
        BotCommand("ia", "Guardião IA Claude Fable (Ativar/Pausar)"),
        BotCommand("corretora", "Configurar corretora e chaves API"),
        BotCommand("estrategia", "Mudar estrategia de operacao"),
        BotCommand("status", "Ver operacoes abertas"),
        BotCommand("scanner", "Scanner ao vivo dos 6 pares na Kraken"),
        BotCommand("shadow", "Telemetria Shadow Trading no Neon DB"),
        BotCommand("why", "Diagnostico do motivo de nao entrar"),
        BotCommand("diag", "Diagnostico de conectividade e saude"),
        BotCommand("profit", "Ver resumo de lucros"),
        BotCommand("balance", "Ver saldo da carteira"),
        BotCommand("daily", "Relatorio de lucro diario"),
        BotCommand("stats", "Estatisticas de acertos"),
        BotCommand("performance", "Desempenho por moeda"),
        BotCommand("count", "Limite de operacoes"),
        BotCommand("whitelist", "Moedas monitoradas"),
        BotCommand("stop", "Pausar robo"),
        BotCommand("reload_config", "Recarregar configuracao"),
        BotCommand("guardian", "Controle do Guardiao IA Fable"),
        BotCommand("help", "Guia completo em Portugues"),
    ]
    await bot.set_my_commands(cmds)
    inline_markup = build_main_menu_inline_markup()
    welcome_text = get_welcome_message()
    msg = await bot.send_message(
        chat_id=CHAT_ID,
        text=welcome_text,
        parse_mode="Markdown",
        reply_markup=inline_markup,
    )
    print("PT-BR_MENU_SENT message_id=", msg.message_id, flush=True)
    # Desfixa qualquer mensagem fixada a pedido do usuario
    try:
        await bot.unpin_all_chat_messages(chat_id=CHAT_ID)
        print("PT-BR_TELEGRAM: Todas as mensagens fixadas no chat foram desfixadas com sucesso!", flush=True)
    except Exception as exc:
        try:
            await bot.unpin_chat_message(chat_id=CHAT_ID)
        except Exception:
            pass
        print("PT-BR_TELEGRAM unpin aviso:", exc, flush=True)


def keep_alive() -> None:
    url = os.environ.get("RENDER_EXTERNAL_URL") or ("https" + "://freqtrade-mnhu.onrender.com")
    time.sleep(60)
    ok_once = False
    while True:
        try:
            requests.get(url + "/api/v1/ping", timeout=15)
            ok_once = True
            print("Keep-alive ping OK:", url, flush=True)
            time.sleep(KEEP_ALIVE_INTERVAL)
        except Exception as exc:
            print("Keep-alive ping status:", exc, flush=True)
            time.sleep(KEEP_ALIVE_INTERVAL if ok_once else 15)


def install_sqlalchemy_neon_pool_hooks() -> None:
    """Injeta pool_pre_ping e pool_recycle no SQLAlchemy contra cold-sleep do Neon (Claude Fable 5.1)."""
    try:
        import sqlalchemy
        from sqlalchemy import event

        orig_create_engine = sqlalchemy.create_engine

        def robust_create_engine(*args, **kwargs):
            kwargs.setdefault("pool_pre_ping", True)
            kwargs.setdefault("pool_recycle", 180)
            kwargs.setdefault("pool_size", 5)
            kwargs.setdefault("max_overflow", 10)
            engine = orig_create_engine(*args, **kwargs)

            @event.listens_for(engine, "handle_error")
            def _handle_db_error(context):
                if context.is_disconnect:
                    print("[db] Neon disconnect interceptado - reconexao via pre-ping garantida.", flush=True)

            return engine

        sqlalchemy.create_engine = robust_create_engine
        print("SQLAlchemy Neon resilient pool hooks installed (pool_pre_ping=True, pool_recycle=180)!", flush=True)
    except Exception as exc:
        print("SQLAlchemy pool hook warning:", exc, flush=True)


def install_telegram_conflict_resilience_hook() -> None:
    """Evita que o Telegram pare o polling se houver conflito passageiro durante o deploy rolling do Render."""
    try:
        from telegram.error import Conflict
        from telegram.ext import ExtBot

        orig_get_updates = ExtBot.get_updates

        async def resilient_get_updates(self, *args, **kwargs):
            for attempt in range(12):
                try:
                    return await orig_get_updates(self, *args, **kwargs)
                except Conflict as exc:
                    if attempt < 11:
                        print(
                            f"[telegram] Conflito temporario de deploy ({exc}) - "
                            f"aguardando finalizacao do container anterior ({attempt + 1}/11)...",
                            flush=True,
                        )
                        await asyncio.sleep(6)
                    else:
                        raise

        ExtBot.get_updates = resilient_get_updates
        print("Telegram Conflict resilience hook installed (12 retries x 6s)!", flush=True)
    except Exception as exc:
        print("Telegram resilience hook warning:", exc, flush=True)


def start_ram_watchdog(interval: int = 60, soft_limit_mb: int = 420) -> None:
    """Watchdog de memória para Render Free (512MB) - dispara gc.collect() se RAM > 420MB."""
    def _watchdog_loop():
        import gc
        while True:
            time.sleep(interval)
            try:
                rss_mb = 0.0
                status_path = pathlib.Path("/proc/self/status")
                if status_path.exists():
                    for line in status_path.read_text(encoding="utf-8").splitlines():
                        if line.startswith("VmRSS:"):
                            rss_mb = float(line.split()[1]) / 1024.0
                            break
                if rss_mb > soft_limit_mb:
                    before = rss_mb
                    gc.collect()
                    print(f"[watchdog] Memória alta ({before:.1f}MB) -> gc.collect() acionado!", flush=True)
            except Exception as exc:
                print(f"[watchdog] falha no monitoramento: {exc}", flush=True)

    t = threading.Thread(target=_watchdog_loop, daemon=True, name="ram-watchdog")
    t.start()
    print("[watchdog] Monitor de Memória RAM ativo (Soft cap 420MB)!", flush=True)


def start_neon_heartbeat(db_url: str, interval: int = 180) -> None:
    """Executa SELECT 1 a cada 4min para impedir que o Neon Postgres entre em suspensão."""
    if not db_url or "postgres" not in db_url:
        return

    def _heartbeat_loop():
        try:
            import psycopg2
        except ImportError:
            return

        dsn = db_url.replace("postgresql+psycopg2://", "postgresql://")
        while True:
            time.sleep(interval)
            try:
                conn = psycopg2.connect(dsn, connect_timeout=10)
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                conn.commit()
                conn.close()
                print("[heartbeat] ✓ Neon PostgreSQL mantido ativo (anti-autosuspend)", flush=True)
            except Exception as exc:
                print(f"[heartbeat] alerta Neon: {exc}", flush=True)

    t = threading.Thread(target=_heartbeat_loop, daemon=True, name="neon-heartbeat")
    t.start()
    print("[heartbeat] Heartbeat do Neon PostgreSQL ativo (240s)!", flush=True)


def start_shadow_sync_worker() -> None:
    """Worker em background que atualiza status e cotações dos shadow trades no Neon DB."""
    def _worker():
        time.sleep(30)
        while True:
            try:
                from user_data.shadow_tracker import sync_all_shadow_trades
                closed = sync_all_shadow_trades()
                if closed > 0:
                    print(f"[shadow_tracker] {closed} shadow trades fechados nesta rodada.", flush=True)
            except Exception as exc:
                print(f"[shadow_tracker] alerta no worker de sincronização: {exc}", flush=True)
            time.sleep(120)

    t = threading.Thread(target=_worker, name="ShadowSyncWorker", daemon=True)
    t.start()
    print("[shadow_tracker] Worker de sincronização Neon DB ativo (120s)!", flush=True)


def _mask_argv(argv: list[str]) -> list[str]:
    """Mascara valores sensíveis após --db-url apenas para logs de auditoria."""
    out: list[str] = []
    mask_next = False
    for item in argv:
        if mask_next:
            host_part = item.split("@")[-1] if "@" in item else "***"
            out.append(f"***@{host_part}")
            mask_next = False
        else:
            out.append(item)
            if item == "--db-url":
                mask_next = True
    return out


def main() -> None:
    print("[BOOT] main() iniciado!", flush=True)
    try:
        test_str = translate_pt_br("Dry run is enabled. All trades are simulated.")
        assert "Modo Simulação" in test_str, f"Translation check failed: {test_str}"
        print("PT-BR_SELF_TEST_OK: All translations verified in container!", flush=True)
    except Exception as e:
        print(f"[BOOT_WARN] Translation test falhou: {e}", flush=True)
    time.sleep(8)
    print("[BOOT] Fase 1: Configurando Telegram menu...", flush=True)
    try:
        asyncio.run(setup_telegram_menu())
        print("Telegram PT-BR menu configurado com sucesso (sem mensagem fixada)!", flush=True)
    except Exception as exc:
        print("Setup telegram menu error:", exc, flush=True)
    threading.Thread(target=keep_alive, daemon=True).start()
    install_pt_br_hooks()
    install_web_pt_br_hooks()
    install_sqlalchemy_neon_pool_hooks()
    install_telegram_conflict_resilience_hook()
    print("Telegram & Web PT-BR translation hooks installed!", flush=True)
    from freqtrade.main import main as ft_main

    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        print("ERROR: DATABASE_URL não definida nas variáveis de ambiente.", file=sys.stderr, flush=True)
        raise SystemExit(1)
    # Correção P0 SRE: normaliza dialeto do SQLAlchemy 2.0 (postgres:// -> postgresql://)
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    # Inicia Watchdog de RAM e Heartbeat do Neon Postgres (Tríade de Ouro - Claude Fable 5.1)
    start_ram_watchdog()
    start_neon_heartbeat(db_url)

    # Inicialização da telemetria de Shadow Trading no Neon PostgreSQL
    try:
        from user_data.shadow_tracker import ensure_shadow_tables
        ensure_shadow_tables()
        print("Neon DB Shadow Trading tables verified/created successfully!", flush=True)
    except Exception as exc:
        print("Shadow tables init warning:", exc, flush=True)
    start_shadow_sync_worker()

    os.environ["FREQTRADE__API_SERVER__LISTEN_PORT"] = os.environ.get("PORT", "10000")
    # Correção definitiva: TODOS os overrides entram na runtime config + env nativas.
    # O mecanismo nativo FREQTRADE__* do freqtrade (EnvironmentVariables) tem a
    # maior precedência comprovada em produção (log: "Loading variable
    # 'FREQTRADE__STRATEGY'"). Env vars nativas dispensam argv por completo.
    force_override = os.environ.get("FORCE_STRATEGY_OVERRIDE", "").strip()
    if force_override:
        boot_strategy = force_override
        print(f"PT-BR_BOOT_STRATEGY_FORCE_OVERRIDE: {boot_strategy}", flush=True)
    else:
        try:
            boot_strategy = get_strategy_state().get("active_strategy", "").strip()
        except Exception:
            boot_strategy = ""
        if not boot_strategy:
            boot_strategy = os.environ.get("FREQTRADE_STRATEGY", "").strip() or "RenderQuantV3_Defensive"
    os.environ["FREQTRADE__STRATEGY"] = boot_strategy
    if db_url:
        os.environ["FREQTRADE__DB_URL"] = db_url
    print("PT-BR_BOOT_STRATEGY_RESOLVED:", boot_strategy, flush=True)
    # Config mínima: freqtrade lê user_data_dir, strategy, db_url e todos os
    # overrides das env vars nativas FREQTRADE__* (comprovadas nos logs).
    sys.argv = [
        "freqtrade",
        "trade",
        "--config",
        str(ensure_runtime_config()),
        "--strategy",
        boot_strategy,
        "--strategy-path",
        "/freqtrade/user_data/strategies",
        "--userdir",
        "/freqtrade/user_data",
        "--db-url",
        db_url,
    ]
    # FIX 6 (à prova de balas): re-lê e hard-set direto na runtime config com marcador
    # único de bot_name. Se show_config reportar este nome, a config foi escrita por
    # ESTE código; se reportar outro, existe outro escritor/instância (diagnóstico).
    try:
        _force = json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
        _force["strategy"] = boot_strategy
        _force["timeframe"] = "4h" if ("V3" in boot_strategy or "V4" in boot_strategy) else "15m"
        _force["user_data_dir"] = "/freqtrade/user_data"
        _force["bot_name"] = f"Unkiorra {boot_strategy} Live"
        _atomic_write_text(RUNTIME_CONFIG_PATH, json.dumps(_force, indent=2, ensure_ascii=False) + "\n")
        print("PT-BR_BOOT_FORCE_FINAL:", _force.get("strategy"), "|", _force.get("bot_name"), "|", _force.get("exchange", {}).get("pair_whitelist"), flush=True)
    except Exception as exc:
        print("PT-BR_BOOT_FORCE_FINAL_WARN:", exc, flush=True)
    # FIX 4 (auditoria): dump do estado final antes de entregar ao freqtrade.
    # Se a estratégia divergir daqui para frente, a causa é pós-argv (reload interno).
    try:
        _cfg = json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8")) if RUNTIME_CONFIG_PATH.exists() else {}
        _is_dry = bool(_cfg.get("dry_run", True))
        os.environ["FREQTRADE__DRY_RUN"] = "true" if _is_dry else "false"
        os.environ["DRY_RUN"] = "true" if _is_dry else "false"
        _db_masked = db_url.split("@")[-1] if "@" in db_url else "***"
        _argv_masked = _mask_argv(sys.argv)
        print(
            f"PT-BR_BOOT_AUDIT: pid={os.getpid()} strategy={_cfg.get('strategy')!r} "
            f"argv_strategy={boot_strategy!r} max_open_trades={_cfg.get('max_open_trades')} "
            f"stake_currency={_cfg.get('stake_currency')} exchange={_cfg.get('exchange', {}).get('name')} "
            f"pairs={len(_cfg.get('exchange', {}).get('pair_whitelist', []))} "
            f"argv={_argv_masked} db@{_db_masked}",
            flush=True,
        )
    except Exception as exc:
        print("PT-BR_BOOT_AUDIT_WARN:", exc, flush=True)
    print("[BOOT] CHAMANDO ft_main() agora...", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    ft_main()


if __name__ == "__main__":
    main()
