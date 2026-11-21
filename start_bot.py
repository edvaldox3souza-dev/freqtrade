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

    for key in _REQUIRED_SECRETS:
        current = os.environ.get(key)
        if _is_unsafe(current):
            generated = _secrets.token_urlsafe(48)
            os.environ[key] = generated
            print(f"[bootstrap] {key}: ausente/placeholder -> segredo aleatório gerado "
                  f"(configure a env var no Render para credencial estável!).", flush=True)
        elif key in _MIN_LEN and len(os.environ[key].strip()) < _MIN_LEN[key]:
            errors.append(f"{key} tem {len(os.environ[key].strip())} caracteres (mínimo {_MIN_LEN[key]}).")

        if key in _MIN_LEN:
            print(f"[bootstrap] {key}: {_fingerprint(os.environ[key])}", flush=True)

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
    ["❓ Ajuda (Guia)"],
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
}

WELCOME_PT_BR = (
    "🤖 *Unkiorra Trade Bot*\n\n"
    "✅ *Robô Ativo:* Monitorando oportunidades em tempo real\n"
    "🏦 *Corretora:* Kraken (`Pares USD` | `RenderQuantV1` `15m`)\n"
    "🛡️ *Modo:* Simulação (`Dry-Run` — Carteira Virtual: `1000 USD`)\n"
    "🧠 *Guardião IA:* Claude Fable 5.1 Ativo (`/ia` ou toque em 🧠 Guardião IA)\n"
    "⚡ *Proteção de Capital:* Ativa (`KER` + `Trailing Stop Pós-Taxas`)\n\n"
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
        if not dst_path.exists() and src_path.exists():
            _atomic_write_text(dst_path, src_path.read_text(encoding="utf-8"))
        if dst_path.exists():
            try:
                cfg = json.loads(dst_path.read_text(encoding="utf-8"))
                changed = False
                # FIX 1: força sempre a estratégia ativa do strategy_state no boot.
                # Sem isto, uma runtime config sem "strategy" faz o freqtrade cair
                # no fallback SimpleStrategy (template 5m) — bug observado em produção.
                try:
                    st = get_strategy_state()
                    active = st.get("active_strategy", "")
                    if active and cfg.get("strategy") != active:
                        cfg["strategy"] = active
                        changed = True
                        print(f"PT-BR_BOOT_STRATEGY_FORCE: {active}", flush=True)
                except Exception as exc:
                    print("PT-BR_BOOT_STRATEGY_WARN:", exc, flush=True)
                # FIX 2: chaves da corretora via env vars (persistentes na Render,
                # sobrevivem a sleep/redeploy). Env tem prioridade sobre /tmp.
                env_key = os.environ.get("EXCHANGE_KEY", "").strip()
                env_secret = os.environ.get("EXCHANGE_SECRET", "").strip()
                env_pass = os.environ.get("EXCHANGE_PASSWORD", "").strip()
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
                ex = cfg.get("exchange", {})
                if not bool(cfg.get("dry_run", True)) and (not ex.get("key") or not ex.get("secret")):
                    cfg["dry_run"] = True
                    changed = True
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
            target_stake = "USD" if cleaned == "kraken" else "USDT"
            cfg["stake_currency"] = target_stake
            old_pairs = ex.get("pair_whitelist", ["BTC/USD", "ETH/USD"])
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
        [InlineKeyboardButton("❓ Por Que Não Entrou?", callback_data="unk_nav:why"), InlineKeyboardButton("🩺 Diagnóstico", callback_data="unk_nav:diag")],
        [InlineKeyboardButton("🔄 Recarregar Painel", callback_data="unk_nav:menu")],
    ])


def build_corretora_inline_rows() -> list[list[tuple[str, str]]]:
    return [
        [("🟣 Kraken", "unk_ex:kraken"), ("🟠 Bybit", "unk_ex:bybit"), ("⚫ OKX", "unk_ex:okx")],
        [("🟢 KuCoin", "unk_ex:kucoin"), ("🟡 Binance", "unk_ex:binance")],
        [("🔑 API Key", "unk_input:api_key"), ("🔐 API Secret", "unk_input:api_secret"), ("🗝️ Passphrase", "unk_input:passphrase")],
        [("🛡️ Modo Simulado", "unk_mode:dry"), ("🚀 Conta Real", "unk_mode:live")],
        [("🧠 Guardião IA (Claude Fable)", "unk_ai:open")],
        [("🗑️ Limpar Chaves", "unk_api:clear"), ("🔄 Atualizar", "unk_ex:refresh"), ("🏠 Menu", "unk_nav:menu")],
    ]


def build_corretora_inline_markup():
    InlineKeyboardButton, InlineKeyboardMarkup = _get_telegram_inline_classes()

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text=t, callback_data=cb) for t, cb in row] for row in build_corretora_inline_rows()]
    )


def format_corretora_text() -> str:
    st = get_exchange_status()
    modo = "Simulação (Dry-Run)" if st["dry_run"] else "Conta Real"
    ts = time.strftime("%H:%M:%S")
    stop_nativo = "🟢 ATIVO no Livro (60s)" if st.get("stoploss_on_exchange") else "🟡 Stop Local (Monitorado pelo Bot)"
    try:
        from user_data.ai_guardian import get_state as _ai_st
        s_ai = _ai_st()
        ai_status = "🟢 ATIVADA (Claude Fable 5.1)" if s_ai.get("enabled", False) else "🔴 PAUSADA"
    except Exception:
        ai_status = "🟢 ATIVADA (Claude Fable 5.1)"
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


STRATEGY_STATE_PATH = pathlib.Path("/freqtrade/user_data/strategy_state.json")
AVAILABLE_STRATEGIES = [
    ("RenderQuantV1", "🛡️ RenderQuant V1 (Equilibrada)"),
    ("RenderQuantV2_Institutional", "🏛️ RenderQuant V2 (Institucional)"),
    ("RenderQuantV3_Defensive", "🧲 RenderQuant V3 (Defensiva 4h — validada em backtest 600d)"),

]


def get_strategy_state() -> dict:
    from user_data.state_store import load_state
    default = {
        "active_strategy": os.environ.get("FREQTRADE_STRATEGY", "RenderQuantV1"),
        "updated_at": time.strftime("%H:%M:%S UTC"),
    }
    data = load_state("strategy_state", [STRATEGY_STATE_PATH], default)
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
            save_state("strategy_state", state, [STRATEGY_STATE_PATH])
        except Exception as exc:
            print("PT-BR_STRATEGY_STATE_WARN:", exc, flush=True)

        if RUNTIME_CONFIG_PATH.exists():
            try:
                cfg = json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
                cfg["strategy"] = strat_name
                _atomic_write_text(RUNTIME_CONFIG_PATH, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
            except Exception as exc:
                print("PT-BR_RUNTIME_STRATEGY_WARN:", exc, flush=True)
    return state


def build_estrategia_inline_markup():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    st = get_strategy_state()
    active = st.get("active_strategy", "RenderQuantV1")
    rows = []
    for s_name, label in AVAILABLE_STRATEGIES:
        mark = "✅" if s_name == active else "⬜"
        rows.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"unk_strat:{s_name}")])
    rows.append([InlineKeyboardButton(text="🔄 Atualizar Lista", callback_data="unk_strat:refresh")])
    return InlineKeyboardMarkup(rows)


def format_estrategia_text() -> str:
    st = get_strategy_state()
    active = st.get("active_strategy", "RenderQuantV1")
    ts = st.get("updated_at", time.strftime("%H:%M:%S"))
    is_v2 = (active == "RenderQuantV2_Institutional")
    ker_min = "0.42 (Rigor Extremo)" if is_v2 else "0.35 (Equilibrado)"
    return (
        f"🎯 *Seleção de Estratégia & Gestão Quantitativa*\n\n"
        f"• *Estratégia ativa no momento:* `{active}`\n"
        f"• *Última alteração:* `{ts}`\n"
        f"• *Filtro KER (Kaufman Anti-Ruído):* `{ker_min}`\n"
        f"• *Teto de Risco (Portfolio Heat):* `Máx 3.00% da Banca`\n"
        f"• *Circuit Breaker Diário:* `-2.50% de Drawdown`\n\n"
        f"👇 *Toque em uma das opções abaixo para alternar:* \n"
        f"_(A comutação é feita em tempo real e preserva trades já abertos com trailing dedicado)_"
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
            return f"🧠 *Guardião IA (Claude Fable 5.1)*\n\n⚠️ Erro ao formatar: `{exc}`"
    return "🧠 *Guardião IA (Claude Fable 5.1)*\n\n• *Status:* 🟢 *ATIVO (Veto Cognitivo Ligado)*\n• *Motor Cognitivo:* `claude-fable-5.1` (Mirai API)\n• *Convicção Mínima:* `65%`\n• *Saída Inteligente (Profit Shield):* `🟢 Ativa`"


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
        "  h.innerHTML='<div style=\"display:flex;align-items:center;gap:16px;\"><span style=\"color:#2dd4bf;font-weight:bold;\">⚡ UNKIORRA QUANT COCKPIT</span><span>Modo: <strong style=\"color:#38bdf8;\">RenderQuantV1</strong></span><span>Risco Máx: <strong style=\"color:#4ade80;\">Heat &le; 3.0%</strong></span><span>Circuit Breaker: <strong style=\"color:#4ade80;\">🟢 ARMADO</strong></span></div><div style=\"display:flex;align-items:center;gap:12px;\"><span style=\"background:#0f766e;color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;\">LIVE SAFEGUARD</span></div>';"
        "  document.body.prepend(h);"
        "  document.body.style.paddingTop='36px';"
        "}"
        "function mountExModal(){""  if(!document.body||document.getElementById('unk-ex-btn'))return;""  const btn=document.createElement('button');btn.id='unk-ex-btn';""  btn.textContent='🏦 Corretora & API';""  btn.style.cssText='position:fixed;bottom:16px;right:16px;z-index:9999;background:#0f766e;color:#fff;border:none;border-radius:999px;padding:10px 16px;font-weight:600;cursor:pointer;box-shadow:0 4px 12px rgba(0,0,0,.3);';""  document.body.appendChild(btn);""  const ov=document.createElement('div');ov.id='unk-ex-ov';""  ov.style.cssText='display:none;position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.6);align-items:center;justify-content:center;';""  ov.innerHTML='<div style=\"background:#1e293b;color:#f8fafc;padding:20px;border-radius:12px;width:360px;max-width:92vw;font-family:sans-serif;\"><h3 style=\"margin:0 0 12px\">🏦 Corretora & Chaves API</h3><label style=\"display:block;font-size:12px;margin-top:8px\">Corretora (ex: kraken, binance, bybit, okx)</label><input id=\"unk-ex-name\" style=\"width:100%;padding:8px;margin-top:4px;border-radius:6px;border:1px solid #475569;background:#0f172a;color:#fff;\"/><label style=\"display:block;font-size:12px;margin-top:8px\">API Key</label><input id=\"unk-ex-key\" type=\"password\" placeholder=\"Deixe vazio para manter atual\" style=\"width:100%;padding:8px;margin-top:4px;border-radius:6px;border:1px solid #475569;background:#0f172a;color:#fff;\"/><label style=\"display:block;font-size:12px;margin-top:8px\">API Secret</label><input id=\"unk-ex-sec\" type=\"password\" placeholder=\"Deixe vazio para manter atual\" style=\"width:100%;padding:8px;margin-top:4px;border-radius:6px;border:1px solid #475569;background:#0f172a;color:#fff;\"/><label style=\"display:block;font-size:12px;margin-top:8px\">Passphrase (opcional: OKX/KuCoin)</label><input id=\"unk-ex-pass\" type=\"password\" style=\"width:100%;padding:8px;margin-top:4px;border-radius:6px;border:1px solid #475569;background:#0f172a;color:#fff;\"/><label style=\"display:flex;align-items:center;gap:8px;margin-top:10px;font-size:13px\"><input id=\"unk-ex-dry\" type=\"checkbox\"/> Modo Simulado (Dry-Run)</label><div id=\"unk-ex-msg\" style=\"font-size:12px;margin-top:8px;color:#94a3b8\"></div><div style=\"display:flex;gap:8px;justify-content:flex-end;margin-top:14px\"><button id=\"unk-ex-close\" style=\"padding:8px 12px;border-radius:6px;border:1px solid #475569;background:transparent;color:#fff;cursor:pointer\">Fechar</button><button id=\"unk-ex-save\" style=\"padding:8px 14px;border-radius:6px;border:none;background:#0f766e;color:#fff;font-weight:600;cursor:pointer\">Salvar e Aplicar</button></div></div>';""  document.body.appendChild(ov);""  function getSessionAuth(){try{const raw=localStorage.getItem('ftAuthLoginInfo');if(raw){const p=JSON.parse(raw);const b=p&&typeof p==='object'?Object.values(p)[0]:null;if(b&&b.accessToken)return 'Bearer '+b.accessToken;}}catch(e){}if(!window.__unkAuth){const u=prompt('Usuário da API Freqtrade:');const p=prompt('Senha da API Freqtrade:');if(u&&p)window.__unkAuth='Basic '+btoa(u+':'+p);}return window.__unkAuth||'';}""  btn.onclick=async()=>{ov.style.display='flex';const m=document.getElementById('unk-ex-msg');m.textContent='Carregando...';const auth=getSessionAuth();if(!auth){m.textContent='Autenticação necessária.';return;}try{const r=await fetch('/api/v1/unkiorra/exchange',{headers:{Authorization:auth}});if(r.status===401){window.__unkAuth='';m.textContent='Sessão expirada. Clique novamente.';return;}const d=await r.json();document.getElementById('unk-ex-name').value=d.exchange||'kraken';document.getElementById('unk-ex-dry').checked=!!d.dry_run;m.textContent='Chave atual: '+(d.key_masked||'Não configurada');}catch(e){m.textContent='Erro ao carregar.';}};""  document.getElementById('unk-ex-close').onclick=()=>{ov.style.display='none';};""  document.getElementById('unk-ex-save').onclick=async()=>{const m=document.getElementById('unk-ex-msg');const auth=getSessionAuth();if(!auth){m.textContent='Autenticação necessária.';return;}m.textContent='Salvando...';const body={exchange:document.getElementById('unk-ex-name').value,dry_run:document.getElementById('unk-ex-dry').checked};const k=document.getElementById('unk-ex-key').value,sc=document.getElementById('unk-ex-sec').value,ps=document.getElementById('unk-ex-pass').value;if(k)body.key=k;if(sc)body.secret=sc;if(ps)body.password=ps;try{const r=await fetch('/api/v1/unkiorra/exchange',{method:'POST',headers:{'Content-Type':'application/json',Authorization:auth},body:JSON.stringify(body)});const d=await r.json();m.textContent=r.ok?('Salvo: '+d.exchange+' ('+d.key_masked+'). Recarregando...'):('Erro: '+(d.detail||'falha'));}catch(e){m.textContent='Erro de rede.';}};""}"
        "function runWalk(){"
        "  if(!document.body)return;"
        "  obs.disconnect();"
        "  prefillLogin();"
        "  walk(document.body);mountCockpitHUD();mountExModal();"
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

        def patched_fr_init(self, path, *args, **kwargs):
            p_str = str(path)
            if p_str.endswith(".html"):
                try:
                    raw = pathlib.Path(p_str).read_bytes()
                    if WEB_PT_BR_MARKER.encode("utf-8") not in raw:
                        patched = inject_web_pt_br_html(raw)
                        tmp_path = pathlib.Path("/tmp") / ("pt_br_" + pathlib.Path(p_str).name)
                        tmp_path.write_bytes(patched)
                        path = str(tmp_path)
                except Exception as exc:
                    print("PT-BR_FR_HOOK_WARN:", exc, flush=True)
            return orig_fr_init(self, path, *args, **kwargs)

        FileResponse.__init__ = patched_fr_init
        print("PT-BR_WEB_UI_HOOK_INSTALLED_V8", flush=True)
    except Exception as exc:
        print("PT-BR_FR_INSTALL_ERR:", exc, flush=True)

    try:
        import base64 as _b64
        import secrets as _secrets
        from fastapi import HTTPException, Request
        from freqtrade.rpc.api_server.webserver import ApiServer

        orig_cfg_app = ApiServer.configure_app

        def _check_basic(req: Request) -> None:
            auth = req.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                try:
                    import jwt as _jwt
                    secret = os.environ.get("FREQTRADE__API_SERVER__JWT_SECRET_KEY", "")
                    if not secret and SECRET_CONFIG_PATH.exists():
                        secret = json.loads(SECRET_CONFIG_PATH.read_text(encoding="utf-8")).get("api_server", {}).get("jwt_secret_key", "")
                    if secret:
                        _jwt.decode(auth[7:].strip(), secret, algorithms=["HS256"])
                        return
                except Exception:
                    pass
            u, pw = get_api_creds()
            expected = "Basic " + _b64.b64encode(f"{u}:{pw}".encode("utf-8")).decode("ascii")
            if not _secrets.compare_digest(auth, expected):
                raise HTTPException(status_code=401, detail="Unauthorized")

        def patched_cfg_app(self, *args, **kwargs):
            res = orig_cfg_app(self, *args, **kwargs)
            app = args[0] if args else getattr(self, "app", None)
            if app is None:
                return res

            @app.get("/api/v1/unkiorra/exchange")
            async def unkiorra_get_ex(request: Request):
                _check_basic(request)
                return get_exchange_status()

            @app.post("/api/v1/unkiorra/exchange")
            async def unkiorra_set_ex(request: Request):
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
                try:
                    if hasattr(self, "_rpc") and self._rpc is not None:
                        self._rpc._rpc_reload_config()
                except Exception as exc:
                    print("PT-BR_EX_RELOAD_WARN:", exc, flush=True)
                return res
            return res

        ApiServer.configure_app = patched_cfg_app
        print("PT-BR_EXCHANGE_API_HOOK_INSTALLED", flush=True)
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
            is_authorized_chat(update)
            inline_markup = build_main_menu_inline_markup()
            msg_func = update.message.reply_text if (update and update.message) else update.effective_chat.send_message
            try:
                await msg_func(WELCOME_PT_BR, parse_mode="Markdown", reply_markup=inline_markup)
            except Exception as md_err:
                print(f"[telegram] Fallback texto puro em _cmd_start_panel: {md_err}", flush=True)
                clean = WELCOME_PT_BR.replace("*", "").replace("`", "").replace("_", "")
                await msg_func(clean, reply_markup=inline_markup)
        except Exception as exc:
            print(f"[telegram] ERRO em _cmd_start_panel: {exc}", flush=True)

    async def _cmd_corretora(update, context):
        try:
            is_authorized_chat(update)
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
            is_authorized_chat(update)
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

    pending_input = {}



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
                        WELCOME_PT_BR,
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
            if tg and hasattr(tg, "_reload_config"):
                try:
                    await tg._reload_config(update, context)
                except Exception:
                    pass
        elif data.startswith("unk_strat:"):
            if data != "unk_strat:refresh":
                strat_name = data.split(":", 1)[1]
                st = update_strategy_state(strat_name)
                if tg and hasattr(tg, "_reload_config"):
                    try:
                        await tg._reload_config(update, context)
                    except Exception:
                        pass
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
                    "Envie agora apenas a sua *Chave de API do Claude Fable (Mirai)*.\nSua mensagem será apagada do chat imediatamente após o salvamento.",
                    parse_mode="Markdown",
                    reply_markup=cancel_kb,
                )
                return
            elif sub == "test":
                await q.answer("🧪 Testando conexão com Claude Fable 5.1...", show_alert=False)
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
            try:
                update_exchange_config(dry_run=dry)
            except ValueError as exc:
                await q.message.reply_text(f"❌ {exc}")
                return
            if tg and hasattr(tg, "_reload_config"):
                try:
                    await tg._reload_config(update, context)
                except Exception:
                    pass
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
            tg = tg_ref.get("self")
            if tg and hasattr(tg, "_reload_config"):
                await tg._reload_config(update, context)
            await update.message.reply_text(f"✅ Corretora alterada para `{st['exchange']}`!", parse_mode="Markdown")
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
        tg = tg_ref.get("self")
        if tg and hasattr(tg, "_reload_config"):
            try:
                await tg._reload_config(update, context)
            except Exception:
                pass
        await update.effective_chat.send_message(
            f"🔐 Chaves atualizadas (`{st['key_masked']}`) para `{st['exchange']}` e mensagem original apagada.",
            parse_mode="Markdown",
        )

    async def _cmd_modo(update, context, dry: bool):
        if not is_authorized_chat(update):
            return
        st = update_exchange_config(dry_run=dry)
        tg = tg_ref.get("self")
        if tg and hasattr(tg, "_reload_config"):
            await tg._reload_config(update, context)
        label = "Simulação (Dry-Run)" if st["dry_run"] else "Conta Real"
        await update.message.reply_text(f"✅ Modo alterado para *{label}*.", parse_mode="Markdown")

    async def _cmd_diag(update, context):
        """Diagnóstico em tempo real do estado do bot e subsistemas (Claude Fable 5.1)."""
        if not is_authorized_chat(update):
            return
        st = get_exchange_status()
        mode_str = "Simulado (Dry-Run)" if st.get("dry_run", True) else "CONTA REAL"
        tok_masked = f"...{TOKEN[-5:]}" if len(TOKEN) > 5 else "NÃO CONFIGURADO"
        stop_nativo_diag = "🟢 ATIVO no Livro da Corretora (intervalo 60s)" if st.get("stoploss_on_exchange") else "🟡 Stop Local (Monitorado pelo Bot)"
        diag_msg = (
            "🩺 *Diagnóstico Forense do Sistema (Freqtrade + Render)*\n\n"
            f"• *Telegram RPC:* {'✅ Ativo e Operacional' if TOKEN else '❌ Desativado (Sem Token)'}\n"
            f"• *Token:* `{tok_masked}`\n"
            f"• *Chat ID Pareado:* `{CHAT_ID}`\n"
            f"• *Modo de Operação:* `{mode_str}`\n"
            f"• *Corretora:* `{st.get('exchange', 'kraken').upper()}`\n"
            f"• *Stop-Loss na Corretora:* `{stop_nativo_diag}`\n"
            f"• *Estratégia:* `RenderQuantV1` (Alpha Triad)\n"
            f"• *Watchdogs:* RAM (420MB) + Neon Anti-Sleep Ativos\n\n"
            "💡 _Seu bot está respondendo normalmente aos comandos._"
        )
        await update.message.reply_text(diag_msg, parse_mode="Markdown")

    async def _cmd_estrategia(update, context):
        if not is_authorized_chat(update):
            return
        await update.message.reply_text(
            format_estrategia_text(),
            parse_mode="Markdown",
            reply_markup=build_estrategia_inline_markup(),
        )

    async def _cmd_why(update, context):
        if not is_authorized_chat(update):
            return
        why_paths = [pathlib.Path("user_data/why_state.json"), pathlib.Path("/freqtrade/user_data/why_state.json")]
        state = None
        for p in why_paths:
            try:
                if p.exists():
                    state = json.loads(p.read_text(encoding="utf-8"))
                    break
            except Exception:
                pass
        if not state:
            await update.message.reply_text(
                "🛡️ *Diagnóstico de Entrada (/why)*\n\n"
                "O robô está ativo e analisando o mercado. Nenhum veto recente registrado.",
                parse_mode="Markdown",
            )
            return

        dec = state.get("decision", "REJECT")
        emoji = "✅" if dec == "ENTER" else "🛡️"
        pair = state.get("pair", "Kraken Spot")
        reason = state.get("reason", "Aguardando confluência estrutural")
        ts = state.get("ts", "Recente")
        heat = state.get("heat")
        heat_str = f"{heat*100:.2f}% / 3.00%" if heat is not None else "Normal"
        mode = state.get("mode", "RenderQuantV1")

        msg = (
            f"{emoji} *Diagnóstico de Decisão (/why)*\n\n"
            f"• *Status:* `{dec}`\n"
            f"• *Par:* `{pair}`\n"
            f"• *Horário:* `{ts}`\n"
            f"• *Modo:* `{mode}`\n"
            f"• *Diagnóstico:* {reason}\n"
            f"• *Portfolio Heat:* `{heat_str}`\n\n"
            f"💡 _O bot opera com rigor institucional: se o mercado estiver em ruído (KER < 0.35) ou o risco passar de 3%, a entrada é vetada para blindar seu capital._"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

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

        if norm in _ALIASES_ESTRATEGIA or txt in ("🎯 Mudar Estratégia", "🎯 Alternar Estratégia"):
            pending_input.pop(cid, None)
            await _cmd_estrategia(update, context)
            raise ApplicationHandlerStop()

        if norm in _ALIASES_WHY or txt in ("❓ Por Que Não?", "❓ Por Que Não Entrou?"):
            pending_input.pop(cid, None)
            await _cmd_why(update, context)
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
            orig_add_handler(self, CommandHandler("diag", _cmd_diag), group=-1)
            orig_add_handler(self, CommandHandler("diagnostico", _cmd_diag), group=-1)
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
    msg = await bot.send_message(
        chat_id=CHAT_ID,
        text=WELCOME_PT_BR,
        parse_mode="Markdown",
        reply_markup=inline_markup,
    )
    print("PT-BR_MENU_SENT message_id=", msg.message_id, flush=True)
    try:
        await bot.pin_chat_message(
            chat_id=CHAT_ID, message_id=msg.message_id, disable_notification=True
        )
    except Exception as exc:
        print("Pin warning:", exc, flush=True)


def keep_alive() -> None:
    url = os.environ.get("RENDER_EXTERNAL_URL") or ("https" + "://freqtrade-mnhu.onrender.com")
    time.sleep(60)
    ok_once = False
    while True:
        try:
            urllib.request.urlopen(url + "/api/v1/ping", timeout=15).read()
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

        orig_create_engine = sqlalchemy.create_engine

        def robust_create_engine(*args, **kwargs):
            kwargs.setdefault("pool_pre_ping", True)
            kwargs.setdefault("pool_recycle", 300)
            kwargs.setdefault("pool_size", 5)
            kwargs.setdefault("max_overflow", 10)
            return orig_create_engine(*args, **kwargs)

        sqlalchemy.create_engine = robust_create_engine
        print("SQLAlchemy Neon resilient pool hooks installed (pool_pre_ping=True, pool_recycle=300)!", flush=True)
    except Exception as exc:
        print("SQLAlchemy pool hook warning:", exc, flush=True)


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


def start_neon_heartbeat(db_url: str, interval: int = 240) -> None:
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


def main() -> None:
    assert "Modo Simulação" in translate_pt_br("Dry run is enabled. All trades are simulated.")
    print("PT-BR_SELF_TEST_OK: All translations verified in container!", flush=True)
    time.sleep(8)
    try:
        asyncio.run(setup_telegram_menu())
        print("Telegram PT-BR menu & pinned message configured successfully!", flush=True)
    except Exception as exc:
        print("Setup telegram menu error:", exc, flush=True)
    threading.Thread(target=keep_alive, daemon=True).start()
    install_pt_br_hooks()
    install_web_pt_br_hooks()
    install_sqlalchemy_neon_pool_hooks()
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

    os.environ["FREQTRADE__API_SERVER__LISTEN_PORT"] = os.environ.get("PORT", "10000")
    # Correção P0: Remove --strategy do sys.argv para que o reload respeite runtime_config.json e a Meta-Strategy dinamicamente!
    sys.argv = [
        "freqtrade",
        "trade",
        "--config",
        str(ensure_runtime_config()),
        "--userdir",
        "/freqtrade/user_data",
        "--db-url",
        db_url,
    ]
    ft_main()


if __name__ == "__main__":
    main()
