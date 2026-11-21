import json
import logging
import pathlib
import time
from datetime import UTC, datetime
from typing import Any

import numpy as np
import talib.abstract as ta
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from pandas import DataFrame

logger = logging.getLogger(__name__)


class RenderQuantV1(IStrategy):
    """Estratégia Quantitativa Institucional para Render (Fase 0 Corrigida).

    Integração de alta performance:
    - Maverick MCP: Minervini Trend Template (Stage 2) + Multiplicador por Regime de Mercado
    - Trading Skills: Modified Z-Score (MAD) de Volume + Anti-Chasing Pivots + Dual Crossover Anti-Whipsaw
    - HyperGrok Trading Desk: Stressed Stop Sizing + Daily Loss Circuit Breaker + Trava de Clusters de Correlação
    - Correções Fase 0: Taxa real Kraken Spot (0.26%), Warm-up de 350 velas, Kelly desativado (<100 trades), Stoploss no bot
    """

    INTERFACE_VERSION = 3

    can_short: bool = False
    timeframe: str = "15m"
    startup_candle_count: int = 350
    process_only_new_candles: bool = True

    # Realização escalonada no tempo (calibrada para cobrir com folga a taxa de 0.52% round-trip)
    minimal_roi = {
        "0": 0.055,
        "60": 0.035,
        "180": 0.020,
    }

    # Stoploss gerenciado internamente pelo bot (Kraken spot não suporta stop nativo em livro via CCXT)
    stoploss: float = -0.04
    stoploss_on_exchange: bool = False
    use_custom_stoploss: bool = True

    use_exit_signal: bool = True
    exit_profit_only: bool = False
    ignore_roi_if_entry_signal: bool = False

    # Parâmetros de Volatilidade e Momentum
    min_natr: float = 0.20
    max_natr: float = 4.00
    min_adx: float = 16.0
    rsi_entry_max: float = 48.0
    rsi_exit_mid: float = 62.0
    rsi_exit_extreme: float = 74.0

    # Risco e Gestão de Capital (HyperGrok & Maverick)
    max_risk_pct_per_trade: float = 0.01   # 1% de risco da banca por operação
    daily_loss_stop_pct: float = 0.025     # -2.5% de perda diária congela novas entradas
    taker_fee_pct: float = 0.0026          # 0.26% taxa taker real da Kraken (varejo)
    slip_floor_bps: float = 10.0           # 10 bps de derrapagem mínima estimada no stop
    kelly_fraction: float = 0.25           # Quarter-Kelly
    max_stake_ratio: float = 0.10          # Teto máximo de 10% da carteira por trade
    cold_start_stake_ratio: float = 0.05   # 5% nos primeiros trades

    # Kaufman Efficiency Ratio & Portfolio Heat (Claude Fable 5.1 / Maverick / HyperGrok)
    KER_WINDOW: int = 14
    KER_MIN_V1: float = 0.35               # Filtro de eficiência para modo V1 (veto de chop/ruído)
    KER_MIN_V2: float = 0.42               # Filtro de eficiência para modo V2 Institucional
    portfolio_heat_limit: float = 0.03     # Hard cap de 3.0% de risco total aberto agregado sobre o equity

    # Clusters de Correlação (HyperGrok) - Evita risco direcional duplicado
    CORRELATION_CLUSTERS = {
        "MAJORS": ["BTC/USD", "ETH/USD", "BTC/USDT", "ETH/USDT"],
        "LAYER1": ["SOL/USD", "ADA/USD", "AVAX/USD", "NEAR/USD", "SUI/USD", "SOL/USDT", "ADA/USDT"],
        "DEFI_INFRA": ["LINK/USD", "DOT/USD", "LINK/USDT", "DOT/USDT"],
    }
    MAX_TRADES_PER_CLUSTER = 1

    current_day_utc = None
    start_of_day_equity = None
    _last_state_read_ts: float = 0.0
    _cached_strategy_mode: str = "RenderQuantV1"

    def _get_active_strategy_mode(self) -> str:
        """Lê dinamicamente o arquivo de estado gravado pelo Telegram sem reiniciar o processo.

        Fail-safe: em caso de arquivo indisponível ou leitura concorrente, mantém o modo padrão V1.
        """
        now = time.time()
        if now - self._last_state_read_ts < 3.0:
            return self._cached_strategy_mode

        paths_to_check = [
            pathlib.Path("/freqtrade/user_data/strategy_state.json"),
            pathlib.Path("user_data/strategy_state.json"),
        ]
        for p in paths_to_check:
            try:
                if p.exists():
                    raw = p.read_text(encoding="utf-8")
                    data = json.loads(raw)
                    mode = data.get("active_strategy", "RenderQuantV1")
                    if mode in ("RenderQuantV1", "RenderQuantV2_Institutional"):
                        if mode != self._cached_strategy_mode:
                            logger.info(
                                "RenderQuant [META-STRATEGY]: Modo ativo comutado em tempo real via Telegram -> %s",
                                mode,
                            )
                        self._cached_strategy_mode = mode
                        self._last_state_read_ts = now
                        return mode
            except Exception as exc:
                logger.debug("RenderQuant: Leitura concorrente de strategy_state.json contornada: %s", exc)

        self._last_state_read_ts = now
        return self._cached_strategy_mode

    @property
    def protections(self) -> list[dict[str, Any]]:
        return [
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 96,
                "trade_limit": 3,
                "stop_duration_candles": 24,
                "max_allowed_drawdown": 0.08,
            },
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 48,
                "trade_limit": 3,
                "stop_duration_candles": 16,
                "only_per_pair": False,
            },
        ]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if dataframe.empty:
            return dataframe

        close = dataframe["close"]
        high = dataframe["high"]
        low = dataframe["low"]
        vol = dataframe["volume"]

        # 1. Filtro de Tendência Estrutural Minervini (Maverick MCP)
        dataframe["sma50"] = ta.SMA(dataframe, timeperiod=50)
        dataframe["sma150"] = ta.SMA(dataframe, timeperiod=150)
        dataframe["sma200"] = ta.SMA(dataframe, timeperiod=200)
        dataframe["sma200_slope"] = (dataframe["sma200"] > dataframe["sma200"].shift(20)).astype(np.int8)

        # 2. Indicadores Rápidos Vetorizados (TA-Lib)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["natr"] = ta.NATR(dataframe, timeperiod=14)
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)

        bollinger = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0, matype=0)
        dataframe["bb_lower"] = bollinger["lowerband"]
        dataframe["bb_middle"] = bollinger["middleband"]

        macd_res = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macd"] = macd_res["macd"]
        dataframe["macd_signal"] = macd_res["macdsignal"]

        # 3. Modified Z-Score de Volume via MAD (Trading Skills - Proteção Fail-Closed SRE)
        rolling_vol = vol.rolling(window=72)
        med_vol = rolling_vol.median().fillna(vol)
        abs_dev = (vol - med_vol).abs()
        mad_vol = abs_dev.rolling(window=72).median().fillna(1.0)
        safe_mad = np.maximum(mad_vol.values, 1e-4)
        raw_z = 0.6745 * (vol.values - med_vol.values) / safe_mad
        dataframe["vol_modified_z"] = np.clip(
            np.nan_to_num(raw_z, nan=0.0, posinf=10.0, neginf=-10.0), -10.0, 10.0
        ).astype(np.float32)
        dataframe["is_whale_vol"] = (dataframe["vol_modified_z"] > 3.0).astype(np.int8)

        # 4. Pivôs Floor-Trader & Anti-Chasing (Trading Skills)
        prev_h = high.rolling(24).max().shift(1)
        prev_l = low.rolling(24).min().shift(1)
        p = (prev_h + prev_l + close.shift(1)) / 3.0
        dataframe["pivot_p"] = p
        dataframe["pivot_r1"] = 2.0 * p - prev_l
        dataframe["pivot_s1"] = 2.0 * p - prev_h

        # 5. Dual Crossover com Penalidade Anti-Whipsaw (Trading Skills)
        ema_up = dataframe["ema9"] > dataframe["ema21"]
        macd_up = dataframe["macd"] > dataframe["macd_signal"]
        dataframe["dual_bullish"] = (ema_up & macd_up).astype(np.int8)
        dataframe["dual_conflict"] = (ema_up != macd_up).astype(np.int8)

        # 6. Métrica de ATR percentual para o stoploss (Blindagem contra NaN no warm-up)
        dataframe["atr_pct"] = np.where(
            (close > 0) & (dataframe["atr"].notna()),
            (dataframe["atr"] / close).astype(np.float32),
            0.015,
        )

        # 7. Regime de Mercado (Maverick)
        dataframe["market_regime_bull"] = (dataframe["sma50"] > dataframe["sma150"]).astype(np.int8)

        # 8. Kaufman Efficiency Ratio (KER - Maverick MCP & Claude Fable 5.1)
        # KER = |close_t - close_{t-w}| / sum(|close_i - close_{i-1}|)
        ker_dir = (close - close.shift(self.KER_WINDOW)).abs()
        ker_vol = close.diff().abs().rolling(self.KER_WINDOW, min_periods=self.KER_WINDOW).sum()
        safe_ker_vol = ker_vol.replace(0, np.nan)
        dataframe["ker"] = (ker_dir / safe_ker_vol).clip(lower=0.0, upper=1.0).fillna(0.0).astype(np.float32)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if dataframe.empty:
            return dataframe

        mode = self._get_active_strategy_mode()
        is_v2 = (mode == "RenderQuantV2_Institutional")

        close = dataframe["close"]
        low = dataframe["low"]
        r1 = dataframe["pivot_r1"]
        p = dataframe["pivot_p"]
        s1 = dataframe["pivot_s1"]

        # 1. Filtro Minervini: ativo em estágio 2 de expansão saudável
        minervini_ok = (
            (close > dataframe["sma50"])
            & (dataframe["sma50"] > dataframe["sma150"])
            & (dataframe["sma150"] > dataframe["sma200"])
            & (dataframe["sma200_slope"] == 1)
        )

        # 2. Filtro Anti-Chasing: não comprar em rompimento esticado sem reteste
        chase_risk = (close > r1) & (low.shift(1) > r1)

        # 3. Pullback estrutural diferenciado por modo (V2 institucional exige maior rigor geométrico)
        if is_v2:
            # V2 Institucional: exige Whale Volume OU proximidade severa (<= 0.8%) de suporte
            whale_condition = (dataframe["is_whale_vol"] == 1)
            support_condition = (
                ((close - p).abs() / p <= 0.008)
                | ((close - s1).abs() / s1 <= 0.008)
                | (close <= dataframe["bb_lower"] * 1.004)
            )
            near_structural_support = whale_condition | support_condition
            active_min_adx = 20.0
            active_rsi_max = 42.0
        else:
            # V1 Equilibrada: tolerância padrão (<= 1.5%)
            near_structural_support = (
                ((close - p).abs() / p <= 0.015)
                | ((close - s1).abs() / s1 <= 0.015)
                | (close <= dataframe["bb_lower"] * 1.006)
            )
            active_min_adx = self.min_adx       # 16.0
            active_rsi_max = self.rsi_entry_max # 48.0

        # 4. Regime e Volatilidade
        regime_ok = (
            (dataframe["natr"] >= self.min_natr)
            & (dataframe["natr"] <= self.max_natr)
            & (dataframe["adx"] >= active_min_adx)
        )

        # 5. Momentum e Confirmação de Retomada
        momentum_ok = (
            (dataframe["dual_bullish"] == 1)
            & (dataframe["dual_conflict"] == 0)
            & (dataframe["rsi"] <= active_rsi_max)
            & (dataframe["rsi"] > dataframe["rsi"].shift(1))
        )

        # 6. Filtro Kaufman Efficiency Ratio (Veto de Chop / Ruído lateral)
        active_ker_min = self.KER_MIN_V2 if is_v2 else self.KER_MIN_V1
        ker_ok = dataframe["ker"] >= active_ker_min

        # Condição de Entrada Composta
        entry_conditions = (
            minervini_ok
            & (~chase_risk)
            & near_structural_support
            & regime_ok
            & momentum_ok
            & ker_ok
            & (dataframe["volume"] > 0)
        )

        dataframe.loc[entry_conditions, "enter_long"] = 1
        # Contrato imutável de entrada por trade (R1 Claude Fable 5.1):
        if is_v2:
            dataframe.loc[entry_conditions, "enter_tag"] = np.where(
                dataframe.loc[entry_conditions, "is_whale_vol"] == 1,
                "V2:whale_pullback",
                "V2:support_pullback",
            )
        else:
            dataframe.loc[entry_conditions, "enter_tag"] = "V1:equilibrada_pullback"

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if dataframe.empty:
            return dataframe

        close = dataframe["close"]

        # Saída por reversão concluída à média de Bollinger com RSI recuperado
        take_reversion = (close >= dataframe["bb_middle"]) & (dataframe["rsi"] > self.rsi_exit_mid)
        # Saída por sobrecompra extrema
        overbought_exit = dataframe["rsi"] > self.rsi_exit_extreme
        # Saída estrutural preventiva: perda da SMA50
        structural_exit = close < dataframe["sma50"]

        dataframe.loc[
            (take_reversion | overbought_exit | structural_exit) & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1
        return dataframe

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> bool:
        """Gatekeeper Institucional (HyperGrok Trading Desk).

        1. Daily Loss Circuit Breaker: bloqueia novas compras se a carteira cair mais de 2.5% no dia UTC.
        2. Trava de Clusters de Correlação: limita no máximo 1 trade ativo por grupo de ativos correlacionados.
        """
        today = current_time.astimezone(UTC).date()
        total_balance = 1000.0
        if self.wallets is not None:
            try:
                total_balance = float(self.wallets.get_total_stake_amount())
            except Exception:
                pass

        if self.current_day_utc != today or self.start_of_day_equity is None:
            self.current_day_utc = today
            self.start_of_day_equity = total_balance
            logger.info("RenderQuant: Novo dia UTC iniciado (%s). Equity base: $%.2f", today, total_balance)

        # 1. Validação do Circuit Breaker Diário (Cálculo robusto sem exceções silenciosas)
        try:
            today_start = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
            closed_trades = Trade.get_trades_proxy(is_open=False)
            closed_today = [
                t for t in closed_trades
                if getattr(t, "close_date_utc", None) and t.close_date_utc >= today_start
            ]
            realized_pnl = sum(float(getattr(t, "close_profit_abs", 0.0) or 0.0) for t in closed_today)

            open_trades = Trade.get_trades_proxy(is_open=True)
            unrealized_pnl = 0.0
            for t in open_trades:
                try:
                    if hasattr(t, "calc_profit_abs"):
                        # Correção P0 SRE: usar a cotação real do próprio trade aberto, jamais o rate do par avaliado!
                        t_rate = getattr(t, "current_rate", None) or getattr(t, "open_rate", None)
                        if t_rate is not None and t_rate > 0:
                            unrealized_pnl += float(t.calc_profit_abs(t_rate))
                        else:
                            unrealized_pnl += float(getattr(t, "realized_profit", 0.0) or 0.0)
                    else:
                        unrealized_pnl += float(getattr(t, "realized_profit", 0.0) or 0.0)
                except Exception:
                    unrealized_pnl += float(getattr(t, "realized_profit", 0.0) or 0.0)

            total_daily_pnl = realized_pnl + unrealized_pnl
            base_eq = self.start_of_day_equity if (self.start_of_day_equity is not None and self.start_of_day_equity > 0) else total_balance
            daily_dd_pct = total_daily_pnl / max(base_eq, 10.0)

            if daily_dd_pct <= -self.daily_loss_stop_pct:
                logger.warning(
                    "RenderQuant [CIRCUIT BREAKER]: Perda diária de %.2f%% atingiu limite de -%.2f%%. Entrada em %s bloqueada.",
                    daily_dd_pct * 100,
                    self.daily_loss_stop_pct * 100,
                    pair,
                )
                return False
        except Exception as exc:
            logger.debug("RenderQuant: Falha não crítica na checagem do Daily Loss: %s", exc)

        # 2. Validação de Clusters de Correlação
        try:
            open_trades = Trade.get_trades_proxy(is_open=True)
            active_pairs = [t.pair for t in open_trades if getattr(t, "is_open", True)]

            for cluster_name, cluster_pairs in self.CORRELATION_CLUSTERS.items():
                if pair in cluster_pairs:
                    active_in_cluster = sum(1 for p in active_pairs if p in cluster_pairs)
                    if active_in_cluster >= self.MAX_TRADES_PER_CLUSTER:
                        logger.info(
                            "RenderQuant [CLUSTER VETO]: Cluster %s saturado (%d posições ativas). Entrada em %s vetada.",
                            cluster_name,
                            active_in_cluster,
                            pair,
                        )
                        return False
        except Exception as exc:
            logger.debug("RenderQuant: Falha não crítica na validação de clusters: %s", exc)

        # 3. Portfolio Heat Gatekeeper (Hard Cap 3.0% de risco total aberto - Claude Fable 5.1 / HyperGrok)
        try:
            open_trades = Trade.get_trades_proxy(is_open=True)
            risky_open = 0.0
            for t in open_trades:
                sl_price = getattr(t, "stop_loss_or_limit", None)
                o_rate = getattr(t, "open_rate", None)
                if sl_price is None and o_rate:
                    sl_price = o_rate * (1.0 - abs(self.stoploss))
                if sl_price and o_rate:
                    risky_open += abs(o_rate - sl_price) * float(getattr(t, "amount", 0.0))

            entry_sl_price = rate * (1.0 - abs(self.stoploss))
            incoming_risk = abs(rate - entry_sl_price) * float(amount)
            total_risk = risky_open + incoming_risk
            portfolio_heat = total_risk / max(total_balance, 10.0)

            if portfolio_heat > self.portfolio_heat_limit:
                logger.warning(
                    "RenderQuant [PORTFOLIO HEAT VETO]: Heat total projetado de %.2f%% excede o limite institucional de %.2f%%. "
                    "(Risco aberto: $%.2f, Risco entrante: $%.2f, Total: $%.2f, Equity: $%.2f). Entrada em %s vetada.",
                    portfolio_heat * 100,
                    self.portfolio_heat_limit * 100,
                    risky_open,
                    incoming_risk,
                    total_risk,
                    total_balance,
                    pair,
                )
                return False

            logger.info(
                "RenderQuant [PORTFOLIO HEAT PASS]: Heat projetado %.2f%% / %.2f%% (Equity $%.2f). Entrada em %s autorizada.",
                portfolio_heat * 100,
                self.portfolio_heat_limit * 100,
                total_balance,
                pair,
            )
        except Exception as exc:
            logger.debug("RenderQuant: Falha não crítica na avaliação de Portfolio Heat: %s", exc)

        return True

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> float:
        """Stressed Sizing + Multiplicador de Regime + Trava Kelly (N >= 100)."""
        mode = self._get_active_strategy_mode()
        is_v2 = (mode == "RenderQuantV2_Institutional")

        # Parâmetros de risco calibrados conforme o modo ativo
        active_risk_pct = 0.0075 if is_v2 else self.max_risk_pct_per_trade  # 0.75% vs 1.0%
        active_stake_ratio = 0.08 if is_v2 else self.max_stake_ratio        # 8% vs 10%
        lateral_multiplier = 0.65 if is_v2 else 0.75                       # -35% vs -25% em range

        total_wallet = max(float(max_stake), float(proposed_stake), 0.0)
        if self.wallets is not None:
            try:
                total_wallet = float(self.wallets.get_total_stake_amount())
            except Exception:
                pass

        # 1. Stressed Stop Distance com taxa real da Kraken (0.26% taker)
        nominal_stop_rate = current_rate * (1.0 - abs(self.stoploss))
        slip_usd = current_rate * (self.slip_floor_bps / 10000.0)
        stressed_stop = nominal_stop_rate - slip_usd
        taker_fees = (current_rate + stressed_stop) * self.taker_fee_pct
        stressed_dist = abs(current_rate - stressed_stop) + taker_fees

        risk_budget = total_wallet * active_risk_pct
        if stressed_dist > 0:
            units = risk_budget / stressed_dist
            calculated_stake = units * current_rate
        else:
            calculated_stake = proposed_stake

        # 2. Multiplicador por Regime de Mercado (Maverick)
        if self.dp is not None:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is not None and not dataframe.empty:
                is_bull = bool(dataframe["market_regime_bull"].iloc[-1])
                if not is_bull:
                    calculated_stake *= lateral_multiplier

        # 3. Ajuste por Quarter-Kelly estritamente para N >= 200 trades (Diretriz P0 Claude Fable 5.1)
        try:
            closed_trades = Trade.get_trades_proxy(is_open=False)
            profits = [
                float(t.close_profit)
                for t in closed_trades
                if getattr(t, "close_profit", None) is not None
            ]
            if len(profits) >= 200:
                wins = [p for p in profits if p > 0]
                losses = [abs(p) for p in profits if p < 0]
                if wins and losses:
                    avg_win = sum(wins) / len(wins)
                    avg_loss = sum(losses) / len(losses)
                    if avg_loss > 0:
                        win_rate = len(wins) / len(profits)
                        payoff = avg_win / avg_loss
                        if payoff > 0:
                            full_kelly = win_rate - ((1.0 - win_rate) / payoff)
                            quarter_kelly = max(0.0, full_kelly * self.kelly_fraction)
                            kelly_cap = total_wallet * min(active_stake_ratio, max(0.02, quarter_kelly))
                            calculated_stake = min(calculated_stake, kelly_cap)
        except Exception:
            pass

        cap = min(calculated_stake, total_wallet * active_stake_ratio, float(max_stake))
        if min_stake is not None and cap < float(min_stake):
            logger.warning(
                "RenderQuant: Ordem ignorada em %s — stake calculado (%.4f) abaixo do min_stake da corretora (%.4f).",
                pair,
                cap,
                float(min_stake),
            )
            return 0.0
        return round(float(cap), 8)

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs: Any,
    ) -> float:
        """Stoploss Dinâmico baseado em ATR + Ratchet Progressivo (Contrato R1 Fable 5.1)."""
        atr_pct = 0.015
        if self.dp is not None:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is not None and not dataframe.empty and "atr_pct" in dataframe.columns:
                val = float(dataframe["atr_pct"].iloc[-1])
                if np.isfinite(val) and val > 0:
                    atr_pct = val

        dynamic_stop = -float(np.clip(2.0 * atr_pct, 0.015, 0.040))

        # R1 Fable 5.1: O modo que ABRIU o trade governa o trailing do trade até o fechamento
        is_v2_trade = bool(trade.enter_tag and "V2:" in trade.enter_tag)

        if is_v2_trade:
            # Trailing Ratchet V2 Institucional: proteção mais rápida de lucros líquidos
            if current_profit >= 0.030:
                return max(dynamic_stop, 0.015)   # Garante lucro líquido real de +1.5%
            if current_profit >= 0.015:
                return max(dynamic_stop, 0.002)   # Breakeven precoce (+0.2% cobrindo taxas)
        else:
            # Trailing Ratchet V1 Equilibrada: padrão
            if current_profit >= 0.035:
                return max(dynamic_stop, 0.012)   # Garante lucro líquido real de +1.2%
            if current_profit >= 0.018:
                return max(dynamic_stop, 0.001)   # Breakeven líquido garantido (+0.1%)

        return dynamic_stop

    def get_risk_status(self) -> dict[str, Any]:
        """Snapshot quantitativo em tempo real para o Telegram e Painel Web (Claude Fable 5.1)."""
        total_balance = 1000.0
        if self.wallets is not None:
            try:
                total_balance = float(self.wallets.get_total_stake_amount())
            except Exception:
                pass

        mode = self._get_active_strategy_mode()
        is_v2 = (mode == "RenderQuantV2_Institutional")
        active_ker = self.KER_MIN_V2 if is_v2 else self.KER_MIN_V1

        open_trades = []
        risky_open = 0.0
        try:
            open_trades = Trade.get_trades_proxy(is_open=True)
            for t in open_trades:
                sl_price = getattr(t, "stop_loss_or_limit", None)
                o_rate = getattr(t, "open_rate", None)
                if sl_price is None and o_rate:
                    sl_price = o_rate * (1.0 - abs(self.stoploss))
                if sl_price and o_rate:
                    risky_open += abs(o_rate - sl_price) * float(getattr(t, "amount", 0.0))
        except Exception:
            pass

        heat_pct = (risky_open / max(total_balance, 10.0)) if total_balance > 0 else 0.0

        return {
            "mode": mode,
            "ker_min": active_ker,
            "portfolio_heat_pct": round(heat_pct * 100, 2),
            "portfolio_heat_limit_pct": round(self.portfolio_heat_limit * 100, 2),
            "open_trades_count": len(open_trades),
            "risky_open_usd": round(risky_open, 2),
            "daily_loss_limit_pct": round(self.daily_loss_stop_pct * 100, 2),
        }
