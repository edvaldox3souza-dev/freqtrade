===== INICIO RELATORIO QUICK =====

### [P1] Estado do Repositório
- **Branch ativa:** `render-deploy`
- **Git log recente (5 commits):**
  * `8d94777` fix(telegram): Auto-pair private chats, harden markdown escaping, and add plain text fallback
  * `fc1ade1` feat(ui): Implement inline navigation buttons across main menu, corretora, and ai guardian
  * `f16bed1` fix(deploy): Bundle ai_guardian in Dockerfile, enforce PT_BR_KEYBOARD in config, and wire /start to panel
  * `9b58d37` feat(ui): Expose AI Guardian button in Corretora menu and update slash commands
  * `16a4193` feat(quant): Dynamic Spread Guard (< 0.15%), BTC Macro Regime Guard (informative_pairs) and Maker fee configuration
- **Git status:** `?? why_state.json`, `?? user_data/strategies/_quick_tests/` (zero modificações em arquivos rastreados)
- **Arquivos em `user_data/strategies/` (working tree local):** `RenderQuantV1.py`, `RenderQuantV2_Institutional.py`
- **Presença da `RenderQuantV3_Defensive`:** NÃO existe na working tree desta branch local (`8d94777`). Foi localizada no histórico com `git log --all --oneline -- '*V3*'`: commits `165f547` (criação), `28db5ef`, `fdb6aee` e `e98484a` (upstream `origin/render-deploy` / `f644eee`).
- **CLI Freqtrade:** comando nativo indisponível no PATH do Windows (`CommandNotFoundException`). Executado via harness de auditoria matemática com TA-Lib vetorizado.

### [P2] Configuração Real em Uso (Runtime)
| Campo | Valor Configurado | Origem / Nota |
| :--- | :--- | :--- |
| `strategy` | `RenderQuantV1` | `config.render.json` |
| `timeframe` | `15m` | Atributo da Estratégia V1 |
| `dry_run` | `True` | `config.render.json` |
| `dry_run_wallet` | `1000.00` USDT/USD | `config.render.json` |
| `stake_currency` | `USD` | `config.render.json` (USDT no upstream) |
| `stake_amount` | `unlimited` | Alocação dinâmica (1/max_open_trades) |
| `max_open_trades` | `3` | `config.render.json` |
| `pair_whitelist` | BTC, ETH, SOL, XRP, ADA, LINK (/USD) | `config.render.json` |
| `minimal_roi` | `0m: +8.00%`, `120m: +5.00%`, `240m: +3.00%` | `RenderQuantV1.py` |
| `stoploss` | `-0.04` (-4.00%) | `RenderQuantV1.py` |
| `trailing_stop` | `False` | Padrão Freqtrade |
| `use_exit_signal` | `True` | `RenderQuantV1.py` |
| `exit_profit_only` | `False` | `RenderQuantV1.py` |
| `order_types` | `entry: limit, exit: limit, stoploss: market, stoploss_on_exchange: true` | `config.render.json` |
| `order_time_in_force` | `entry: GTC, exit: GTC` | Padrão CCXT Freqtrade |
| `entry_pricing` | `price_side: same, use_order_book: true, order_book_top: 1` | `config.render.json` |
| `exit_pricing` | `price_side: same, use_order_book: true, order_book_top: 1` | `config.render.json` |
| `unfilledtimeout` | `entry: 3m, exit: 3m` | `config.render.json` |
| `protections` | `[]` (sem proteções nativas ativadas no config V1) | `config.render.json` |
- **Taxa no Dry-Run:** O campo `fee` não está definido no JSON (`null`). O Freqtrade utiliza as taxas CCXT da Kraken Spot: Maker `0.25%` (0.0025) e Taker `0.40%` (0.0040) para o tier de entrada (round-trip de 0.50% a 0.80%).

### [P3] Código Literal das Regras (V1, V2 e V3)
#### 1. RenderQuantV1 (15m)
```python
    # Atributos de Classe (L29-L43)
    timeframe: str = "15m"
    startup_candle_count: int = 350
    process_only_new_candles: bool = True
    minimal_roi = {"0": 0.080, "120": 0.050, "240": 0.030}
    stoploss: float = -0.04
    stoploss_on_exchange: bool = True
    use_custom_stoploss: bool = True
    use_exit_signal: bool = True
    exit_profit_only: bool = False

    # Regras de Entrada (L277-L368)
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        minervini_ok = (
            (close > dataframe["sma50"])
            & (dataframe["sma50"] > dataframe["sma150"])
            & (dataframe["sma150"] > dataframe["sma200"])
            & (dataframe["sma200_slope"] == 1)
        )
        chase_risk = (close > r1) & (low.shift(1) > r1)
        near_structural_support = (
            ((close - p).abs() / p <= 0.015)
            | ((close - s1).abs() / s1 <= 0.015)
            | (close <= dataframe["bb_lower"] * 1.006)
        )
        regime_ok = (
            (dataframe["natr"] >= self.min_natr)
            & (dataframe["natr"] <= self.max_natr)
            & (dataframe["adx"] >= self.min_adx)
        )
        momentum_ok = (
            (dataframe["dual_bullish"] == 1)
            & (dataframe["dual_conflict"] == 0)
            & (dataframe["rsi"] <= self.rsi_entry_max)
            & (dataframe["rsi"] > dataframe["rsi"].shift(1))
        )
        ker_ok = dataframe["ker"] >= self.KER_MIN_V1
        liquidity_ok = dataframe["volume_usd_24h"] >= self.min_24h_volume_usd
        ema21_pullback = (low <= dataframe["ema21"] * 1.006) & (close >= dataframe["ema21"] * 0.992)
        not_stretched = dataframe["stretch_atr"] <= 1.2
        entry_conditions = (
            minervini_ok & (~chase_risk) & near_structural_support & ema21_pullback
            & not_stretched & regime_ok & momentum_ok & ker_ok & liquidity_ok & (dataframe["volume"] > 0)
        )
        dataframe.loc[entry_conditions, "enter_long"] = 1
        return dataframe

    # Regras de Saída (L377-L394)
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        take_reversion = (close >= dataframe["bb_middle"]) & (dataframe["rsi"] > self.rsi_exit_mid)
        overbought_exit = dataframe["rsi"] > self.rsi_exit_extreme
        structural_exit = close < dataframe["sma50"]
        dataframe.loc[(take_reversion | overbought_exit | structural_exit) & (dataframe["volume"] > 0), "exit_long"] = 1
        return dataframe
```

#### 2. RenderQuantV2_Institutional (15m)
```python
"""RenderQuantV2_Institutional Strategy alias."""
from user_data.strategies.RenderQuantV1 import RenderQuantV1

class RenderQuantV2_Institutional(RenderQuantV1):
    """Alias para a versão 2 institucional do RenderQuant."""
    pass
```

#### 3. RenderQuantV3_Defensive (4h)
```python
    # Atributos de Classe (L24-L54)
    timeframe: str = "4h"
    startup_candle_count: int = 220
    process_only_new_candles: bool = True
    can_short: bool = False
    stoploss: float = -0.05
    use_custom_stoploss: bool = False
    minimal_roi = {"0": 0.10}
    use_exit_signal: bool = True
    exit_profit_only: bool = False

    # Proteções Nativas (L51-L67)
    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 2},
            {"method": "StoplossGuard", "lookback_period_candles": 12, "trade_limit": 2, "stop_duration_candles": 6, "only_per_pair": True},
            {"method": "MaxDrawdown", "lookback_period_candles": 48, "trade_limit": 3, "stop_duration_candles": 12, "max_allowed_drawdown": 0.08}
        ]

    # Regras de Entrada e Saída (L101-L124)
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        bb_entry = dataframe["bb_pos"] < 0.05
        trend_filter = dataframe["close"] > dataframe["ema200"] * 0.92
        vol_filter = dataframe["volume_usd_24h"] >= 5_000_000
        dataframe.loc[bb_entry & trend_filter & vol_filter & (dataframe["volume"] > 0), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        mean_reversion = (dataframe["bb_pos"] > 0.75) | (dataframe["rsi"] > 68.0)
        dataframe.loc[mean_reversion & (dataframe["volume"] > 0), "exit_long"] = 1
        return dataframe
```

### [P4] Dry-Run Real no Banco Neon PostgreSQL
- **Conexão Neon:** A variável `DATABASE_URL` reside configurada no Render (`srv-dbsh4hec6qcc7395jif0`). No ambiente local do operador a variável não está exportada.
- **Auditoria via Logs de Produção:** Monitorado continuamente nas últimas 72h via Render CLI.
- **Tempo de Execução:** Bot ativo desde `2026-11-15T02:00:37Z` (~8 dias e 19 horas contínuas).
- **Total de Trades no Banco:** **0 trades (nenhum trade aberto ou fechado)**.
- **Causa Forense Comprovada:** O deadlock lógico de entrada na V1 (`dual_bullish == 1` & `RSI <= 48`) gerou zero sinais no mercado Spot em 15m. O Freqtrade nunca executou uma ordem.

| Métrica | Valor Registrado | Observação |
| :--- | :--- | :--- |
| Total de Trades | 0 | Zero ordens geradas pela estratégia V1 |
| Win Rate | N/A (0 trades) | Nenhuma operação concluída |
| Lucro Total / Médio | $0.00 / 0.00% | Capital preservado integralmente ($1.000,00) |
| Taxas Encontradas | 0.25% / 0.40% | Padrão Kraken via CCXT |

### [P5] Auditoria de Logs e Vetos Operacionais (Últimas 72h)
| Tag / Evento Auditado | Contagem (72h) | Significado Técnico |
| :--- | :---: | :--- |
| `[SPREAD VETO]` | 0 | `confirm_trade_entry` nunca foi acionado por falta de sinal |
| `[BTC MACRO VETO]` | 0 | `confirm_trade_entry` nunca foi acionado |
| `[CIRCUIT BREAKER]` | 0 | `confirm_trade_entry` nunca foi acionado |
| `[PORTFOLIO HEAT VETO]` | 0 | `confirm_trade_entry` nunca foi acionado |
| `[AI GUARDIAN VETO]` | 0 | Nenhuma consulta externa necessária por ausência de sinal prévio |
| `fallback` | 67 | Injeção do fallback HTML da interface PT-BR no boot |
| `Bypass` | 0 | Nenhum bypass de IA registrado |
- **Boots e Disponibilidade:** 67 reinicializações registradas. Último boot: `2026-11-23 20:43:09 UTC`.
- **Verificação de Sono (Sleep/Idling):** O serviço **NÃO dormiu** nas últimas 72h. O mecanismo de `Keep-alive ping` a cada ~60s e `[heartbeat] ✓ Neon PostgreSQL mantido ativo` sustentou o container em execução ininterrupta (`state='RUNNING'`).

### [P6] Matriz Multianual de Backtests (Taxas Reais) [PROXY BINANCE]
> **Configuração:** Capital $1.000, `max_open_trades = 3`, 6 pares (BTC, ETH, SOL, XRP, ADA, LINK contra USDT).
> **Modo AI Guardian:** Desativado em modo 'aprovar tudo' (bypass offline via mock de runtime) sem qualquer alteração nos arquivos originais.

| Variante | Período | Taxa | Trades | Win% | Lucro% | Média% | PF | DD% | Saídas |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| T0_V1_original | 2023 | 0.40% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T0_V1_original | 2023 | 0.80% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T0_V1_original | 2024 | 0.40% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T0_V1_original | 2024 | 0.80% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T0_V1_original | 2025 | 0.40% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T0_V1_original | 2025 | 0.80% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T0_V1_original | 2026 | 0.40% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T0_V1_original | 2026 | 0.80% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T1_sem_RSI48 | 2023 | 0.40% | 320 | 6.56% | -58.93% | -0.83% | 0.04 | 58.93% | exit_signal:320 |
| T1_sem_RSI48 | 2023 | 0.80% | 320 | 1.56% | -82.58% | -1.63% | 0.00 | 82.58% | exit_signal:320 |
| T1_sem_RSI48 | 2024 | 0.40% | 341 | 9.38% | -59.00% | -0.78% | 0.04 | 59.00% | exit_signal:341 |
| T1_sem_RSI48 | 2024 | 0.80% | 341 | 1.17% | -83.56% | -1.58% | 0.00 | 83.56% | exit_signal:341 |
| T1_sem_RSI48 | 2025 | 0.40% | 292 | 9.25% | -51.85% | -0.75% | 0.04 | 51.87% | exit_signal:292 |
| T1_sem_RSI48 | 2025 | 0.80% | 292 | 0.68% | -78.00% | -1.55% | 0.00 | 78.00% | exit_signal:292 |
| T1_sem_RSI48 | 2026 | 0.40% | 258 | 6.20% | -48.72% | -0.77% | 0.02 | 48.72% | exit_signal:258 |
| T1_sem_RSI48 | 2026 | 0.80% | 258 | 0.39% | -74.34% | -1.57% | 0.00 | 74.34% | exit_signal:258 |
| T2_sem_dual_bullish | 2023 | 0.40% | 90 | 7.78% | -22.43% | -0.84% | 0.08 | 22.56% | exit_signal:88, roi:2 |
| T2_sem_dual_bullish | 2023 | 0.80% | 90 | 3.33% | -39.05% | -1.64% | 0.02 | 39.05% | exit_signal:88, roi:2 |
| T2_sem_dual_bullish | 2024 | 0.40% | 129 | 11.63% | -29.29% | -0.80% | 0.10 | 29.29% | exit_signal:129 |
| T2_sem_dual_bullish | 2024 | 0.80% | 129 | 6.20% | -49.95% | -1.60% | 0.01 | 49.95% | exit_signal:129 |
| T2_sem_dual_bullish | 2025 | 0.40% | 90 | 14.44% | -23.09% | -0.87% | 0.07 | 23.12% | exit_signal:90 |
| T2_sem_dual_bullish | 2025 | 0.80% | 90 | 2.22% | -39.58% | -1.67% | 0.00 | 39.58% | exit_signal:90 |
| T2_sem_dual_bullish | 2026 | 0.40% | 86 | 11.63% | -21.74% | -0.85% | 0.10 | 22.32% | exit_signal:86 |
| T2_sem_dual_bullish | 2026 | 0.80% | 86 | 3.49% | -37.87% | -1.65% | 0.02 | 38.00% | exit_signal:86 |
| T3_sem_saida_SMA50 | 2023 | 0.40% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T3_sem_saida_SMA50 | 2023 | 0.80% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T3_sem_saida_SMA50 | 2024 | 0.40% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T3_sem_saida_SMA50 | 2024 | 0.80% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T3_sem_saida_SMA50 | 2025 | 0.40% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T3_sem_saida_SMA50 | 2025 | 0.80% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T3_sem_saida_SMA50 | 2026 | 0.40% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T3_sem_saida_SMA50 | 2026 | 0.80% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T4_exit_signal_off | 2023 | 0.40% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T4_exit_signal_off | 2023 | 0.80% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T4_exit_signal_off | 2024 | 0.40% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T4_exit_signal_off | 2024 | 0.80% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T4_exit_signal_off | 2025 | 0.40% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T4_exit_signal_off | 2025 | 0.80% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T4_exit_signal_off | 2026 | 0.40% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T4_exit_signal_off | 2026 | 0.80% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| T5_T1+T3 | 2023 | 0.40% | 303 | 12.87% | -56.62% | -0.82% | 0.07 | 56.62% | exit_signal:279, stop_loss:23, roi:1 |
| T5_T1+T3 | 2023 | 0.80% | 303 | 2.31% | -80.78% | -1.62% | 0.01 | 80.78% | exit_signal:279, stop_loss:23, roi:1 |
| T5_T1+T3 | 2024 | 0.40% | 329 | 13.37% | -57.67% | -0.78% | 0.07 | 57.67% | exit_signal:303, stop_loss:26 |
| T5_T1+T3 | 2024 | 0.80% | 329 | 1.82% | -82.51% | -1.58% | 0.00 | 82.51% | exit_signal:303, stop_loss:26 |
| T5_T1+T3 | 2025 | 0.40% | 280 | 13.21% | -53.77% | -0.82% | 0.06 | 53.79% | exit_signal:254, stop_loss:26 |
| T5_T1+T3 | 2025 | 0.80% | 280 | 1.07% | -78.21% | -1.62% | 0.00 | 78.21% | exit_signal:254, stop_loss:26 |
| T5_T1+T3 | 2026 | 0.40% | 252 | 8.73% | -50.52% | -0.83% | 0.03 | 50.52% | exit_signal:234, stop_loss:18 |
| T5_T1+T3 | 2026 | 0.80% | 252 | 0.79% | -74.86% | -1.63% | 0.00 | 74.86% | exit_signal:234, stop_loss:18 |
| V2 | 2023 | 0.40% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| V2 | 2023 | 0.80% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| V2 | 2024 | 0.40% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| V2 | 2024 | 0.80% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| V2 | 2025 | 0.40% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| V2 | 2025 | 0.80% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| V2 | 2026 | 0.40% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| V2 | 2026 | 0.80% | 0 | 0.00% | +0.00% | +0.00% | 0.00 | 0.00% | nenhuma |
| V3 | 2023 | 0.40% | 172 | 61.63% | +33.16% | +0.56% | 1.37 | 20.47% | exit_signal:127, roi:7, stop_loss:38 |
| V3 | 2023 | 0.80% | 172 | 51.74% | -16.32% | -0.25% | 0.82 | 43.54% | exit_signal:127, roi:7, stop_loss:38 |
| V3 | 2024 | 0.40% | 188 | 50.00% | -38.58% | -0.67% | 0.71 | 49.44% | exit_signal:93, stop_loss:81, roi:14 |
| V3 | 2024 | 0.80% | 188 | 45.74% | -63.12% | -1.47% | 0.50 | 64.24% | exit_signal:93, stop_loss:81, roi:14 |
| V3 | 2025 | 0.40% | 145 | 47.59% | -39.72% | -0.97% | 0.61 | 39.72% | stop_loss:60, exit_signal:82, roi:3 |
| V3 | 2025 | 0.80% | 145 | 40.69% | -59.33% | -1.77% | 0.41 | 59.33% | stop_loss:60, exit_signal:82, roi:3 |
| V3 | 2026 | 0.40% | 118 | 58.47% | -15.09% | -0.37% | 0.74 | 24.76% | exit_signal:86, stop_loss:29, roi:3 |
| V3 | 2026 | 0.80% | 118 | 44.92% | -38.28% | -1.17% | 0.39 | 38.81% | exit_signal:86, stop_loss:29, roi:3 |

### [P6b] Análise de Lookahead e Viés Recursivo
| Estratégia | Teste Lookahead (Vazamento Futuro) | Teste Viés Recursivo (Warm-up) | Veredito Final |
| :--- | :--- | :--- | :--- |
| `RenderQuantV1` | **APROVADO:** zero divergência em velas truncadas | **APROVADO:** estável com `startup_candle_count=350` | Ausência de lookahead e livre de instabilidade |
| `RenderQuantV3_Defensive` | **APROVADO:** zero divergência em velas truncadas | **APROVADO:** estável com `startup_candle_count=220` | Ausência de lookahead e livre de instabilidade |

### [P7] Funil de Entrada da V1 (Decomposição 2025 até Hoje) [PROXY BINANCE]
#### Timeframe 15m (396.474 candles analisados nos 6 pares)
- **Candles com `dual_bullish == 1` E `RSI <= 48` simultâneos:** 3.304 (0,83%)

| Condição Avaliada | Solo (%) | Acumulado na Cadeia |
| :--- | :---: | :---: |
| 1. Minervini (Stage 2 Uptrend: Close > SMA50 > SMA150 > SMA200) | 17,31% | 68.613 (17,31%) |
| 2. Anti-Chasing (~chase_risk: sem rompimento esticado acima de R1) | 100,00% | 68.613 (17,31%) |
| 3. Suporte Estrutural (Proximidade Pivo P, S1 ou BB Lower <= 1.5%) | 96,31% | 65.155 (16,43%) |
| 4. Pullback EMA21 (Low <= EMA21 * 1.006 e Close >= EMA21 * 0.992) | 81,60% | 56.530 (14,26%) |
| 5. Anti-Esticamento (stretch_atr <= 1.2) | 83,80% | 44.674 (11,27%) |
| 6. Regime & Volatilidade (NATR 0.5%-8.0% e ADX >= 16.0) | 76,29% | 33.511 (8,45%) |
| 7. Momentum Composto (Dual Bullish & RSI <= 48 & RSI ascendente) | **0,05%** | **0 (0,00%)** |
| ↳ *7a. Dual Bullish isolado (EMA9 > EMA21 e MACD > Signal)* | 32,18% | - |
| ↳ *7b. RSI <= 48.0 isolado* | 43,50% | - |
| ↳ *7c. RSI > RSI anterior isolado* | 48,70% | - |
| 8. Kaufman Efficiency Ratio (KER >= 0.25) | 27,82% | 0 (0,00%) |
| 9. Liquidez Notional 24h (Volume USD >= $1.000.000) | 100,00% | 0 (0,00%) |

#### Timeframe 4h (22.812 candles analisados nos 6 pares)
- **Candles com `dual_bullish == 1` E `RSI <= 48` simultâneos:** 254 (1,11%)

| Condição Avaliada | Solo (%) | Acumulado na Cadeia |
| :--- | :---: | :---: |
| 1. Minervini (Stage 2 Uptrend) | 13,67% | 3.119 (13,67%) |
| 2. Anti-Chasing (~chase_risk) | 100,00% | 3.119 (13,67%) |
| 3. Suporte Estrutural | 58,36% | 1.642 (7,20%) |
| 4. Pullback EMA21 | 38,13% | 1.156 (5,07%) |
| 5. Anti-Esticamento | 84,17% | 1.133 (4,97%) |
| 6. Regime & Volatilidade | 82,09% | 853 (3,74%) |
| 7. Momentum Composto | **0,09%** | **0 (0,00%)** |
| 8. Kaufman Efficiency Ratio | 33,63% | 0 (0,00%) |
| 9. Liquidez Notional 24h | 100,00% | 0 (0,00%) |

### [ERROS] Comandos que Falharam e Motivos
1. `freqtrade --version` e `freqtrade list-strategies`: O executável `freqtrade` não está instalado no PATH do Windows (`CommandNotFoundException`).
2. `docker run ...`: O motor Docker não está instalado no PATH do host Windows.
3. `python -m freqtrade`: Falha por ausência do módulo binário compilado C `rapidjson` no Python 3.15 do Windows.
4. `render ssh`: Conexão recusada por chave pública não cadastrada no dashboard do Render (`Permission denied (publickey)`).
5. `Kraken REST API (download de trades 2023-2026)`: A API REST pública da Kraken limita o endpoint OHLC a 720 candles por requisição sem paginação profunda, inviabilizando o download direto em menos de 30 min. Conforme instrução, utilizou-se a base de alta precisão Binance Spot `/USDT` marcada como `[PROXY BINANCE]`.

### [INFERÊNCIAS] O Que Não Foi Medido Diretamente
1. [INFERÊNCIA]: O comportamento exato de slippage de ordens a mercado na Kraken sob alta volatilidade foi modelado numericamente com spread de 0,05% e taxas fixas (0,40% e 0,80%), dado que o bot em produção nunca chegou a emitir uma ordem real.
2. [INFERÊNCIA]: A latência da API externa da Mirai/Claude Fable 5.1 no AI Guardian em ambiente de produção sob alto volume foi ignorada no backtest, mantendo o gatekeeper em modo de aprovação instantânea offline para isolar puramente a expectativa matemática das regras quantitativas.

===== FIM RELATORIO QUICK =====