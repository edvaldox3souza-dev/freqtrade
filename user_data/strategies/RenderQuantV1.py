import json
import logging
import pathlib
import time
from datetime import UTC, datetime
from typing import Any, Optional, Union

import numpy as np
import talib.abstract as ta
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from pandas import DataFrame

logger = logging.getLogger(__name__)


class RenderQuantV1(IStrategy):
    """Estrat?gia Quantitativa Institucional para Render (Fase 0 Corrigida).

    Integra??o de alta performance:
    - Maverick MCP: Minervini Trend Template (Stage 2) + Multiplicador por Regime de Mercado
    - Trading Skills: Modified Z-Score (MAD) de Volume + Anti-Chasing Pivots + Dual Crossover Anti-Whipsaw
    - HyperGrok Trading Desk: Stressed Stop Sizing + Daily Loss Circuit Breaker + Trava de Clusters de Correla??o
    - Corre??es Fase 0: Taxa real Kraken Spot (0.26%), Warm-up de 350 velas, Kelly desativado (<100 trades), Stoploss no bot
    """

    INTERFACE_VERSION = 3

    can_short: bool = False
    timeframe: str = "15m"
    startup_candle_count: int = 350
    process_only_new_candles: bool = True

    # Realiza??o escalonada no tempo (calibrada para cobrir com folga a taxa de 0.52% round-trip)
    minimal_roi = {
        "0": 0.080,    # Realiza??o ampla em 8.0%
        "120": 0.050,  # 5.0% ap?s 2 horas
        "240": 0.030,  # 3.0% ap?s 4 horas
    }

    # Stop-Loss Nativo no Livro da Corretora (A??o 2 - Claude Fable 5.1)
    stoploss: float = -0.04
    stoploss_on_exchange: bool = True
    use_custom_stoploss: bool = True

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "emergency_exit": "market",
        "force_entry": "market",
        "force_exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": True,
        "stoploss_on_exchange_interval": 60,
        "stoploss_on_exchange_limit_ratio": 0.99,
    }

    use_exit_signal: bool = True
    exit_profit_only: bool = False
    ignore_roi_if_entry_signal: bool = False

    # Par?metros de Volatilidade e Momentum
    min_natr: float = 0.20
    max_natr: float = 4.00
    min_adx: float = 16.0
    rsi_entry_max: float = 48.0
    rsi_exit_mid: float = 62.0
    rsi_exit_extreme: float = 74.0

    # Risco e Gest?o de Capital (HyperGrok & Maverick)
    max_risk_pct_per_trade: float = 0.01   # 1% de risco da banca por opera??o
    daily_loss_stop_pct: float = 0.025     # -2.5% de perda di?ria congela novas entradas
    taker_fee_pct: float = 0.0026          # 0.26% taxa taker real da Kraken (varejo)
    slip_floor_bps: float = 10.0           # 10 bps de derrapagem m?nima estimada no stop
    kelly_fraction: float = 0.25           # Quarter-Kelly
    max_stake_ratio: float = 0.10          # Teto m?ximo de 10% da carteira por trade
    cold_start_stake_ratio: float = 0.05   # 5% nos primeiros trades

    # Kaufman Efficiency Ratio & Portfolio Heat (Claude Fable 5.1 / Maverick / HyperGrok)
    KER_WINDOW: int = 14
    KER_MIN_V1: float = 0.35               # Filtro de efici?ncia para modo V1 (veto de chop/ru?do)
    KER_MIN_V2: float = 0.42               # Filtro de efici?ncia para modo V2 Institucional
    portfolio_heat_limit: float = 0.03     # Hard cap de 3.0% de risco total aberto agregado sobre o equity
    min_24h_volume_usd: float = 2_000_000.0 # M?nimo $2M USD negociados nas ?ltimas 24h (anti-slippage)

    # Clusters de Correla??o (HyperGrok) - Evita risco direcional duplicado nos 6 pares l?quidos
    CORRELATION_CLUSTERS = {
        "MAJORS": ["BTC/USD", "ETH/USD", "BTC/USDT", "ETH/USDT"],
        "LAYER1": ["SOL/USD", "ADA/USD", "SOL/USDT", "ADA/USDT"],
        "DEFI_INFRA": ["LINK/USD", "LINK/USDT"],
        "PAYMENTS": ["XRP/USD", "XRP/USDT"],
    }
    MAX_TRADES_PER_CLUSTER = 1

    current_day_utc = None
    start_of_day_equity = None
    _last_state_read_ts: float = 0.0
    _cached_strategy_mode: str = "RenderQuantV1"

    WHY_STATE_FILE = pathlib.Path("user_data/why_state.json")

    def _record_decision(
        self,
        pair: str,
        decision: str,
        reason: str,
        ker: float | None = None,
        heat: float | None = None,
        cluster: str | None = None,
    ) -> None:
        """Persiste em disco a ?ltima decis?o/rejei??o para o comando /why do Telegram (Claude Fable 5.1)."""
        payload = {
            "ts": datetime.now(UTC).strftime("%H:%M:%S UTC"),
            "pair": pair,
            "decision": decision,
            "reason": reason,
            "ker": ker,
            "heat": heat,
            "cluster": cluster,
            "mode": self._cached_strategy_mode,
        }
        try:
            from user_data.state_store import save_state
            save_state("why_state", payload, [self.WHY_STATE_FILE, pathlib.Path("/freqtrade/user_data/why_state.json")])
        except Exception:
            pass

    def _get_active_strategy_mode(self) -> str:
        """L? dinamicamente o arquivo de estado gravado pelo Telegram sem reiniciar o processo.

        Fail-safe: em caso de arquivo indispon?vel ou leitura concorrente, mant?m o modo padr?o V1.
        """
        now = time.time()
        if now - self._last_state_read_ts < 3.0:
            return self._cached_strategy_mode

        try:
            from user_data.state_store import load_state
            data = load_state(
                "strategy_state",
                [
                    pathlib.Path("/freqtrade/user_data/strategy_state.json"),
                    pathlib.Path("user_data/strategy_state.json"),
                ],
                {"active_strategy": "RenderQuantV1"},
            )
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
            logger.debug("RenderQuant: Falha ao ler strategy_state (fallback V1): %s", exc)

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

    def informative_pairs(self) -> list[tuple[str, str]]:
        """Garante que o par do BTC sempre seja baixado para o Macro Regime Guard (Claude Fable 5.1)."""
        stake = "USD"
        try:
            if hasattr(self, "config") and self.config:
                stake = self.config.get("stake_currency", "USD")
        except Exception:
            pass
        return [(f"BTC/{stake}", self.timeframe)]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if dataframe.empty:
            return dataframe

        close = dataframe["close"]
        high = dataframe["high"]
        low = dataframe["low"]
        vol = dataframe["volume"]

        # 1. Filtro de Tend?ncia Estrutural Minervini (Maverick MCP)
        dataframe["sma50"] = ta.SMA(dataframe, timeperiod=50)
        dataframe["sma150"] = ta.SMA(dataframe, timeperiod=150)
        dataframe["sma200"] = ta.SMA(dataframe, timeperiod=200)
        dataframe["sma200_slope"] = (dataframe["sma200"] > dataframe["sma200"].shift(20)).astype(np.int8)

        # 2. Indicadores R?pidos Vetorizados (TA-Lib)
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

        # 3. Modified Z-Score de Volume via MAD (Trading Skills - Prote??o Fail-Closed SRE)
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

        # 4. Piv?s Floor-Trader & Anti-Chasing (Trading Skills)
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

        # 6. M?trica de ATR percentual para o stoploss (Blindagem contra NaN no warm-up)
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

        # 9. Liquidez Financeira Rolante 24h (Anti-Slippage - Claude Fable 5.1)
        # 96 velas de 15m = 24h
        vol_notional_24h = (dataframe["volume"] * close).rolling(96, min_periods=16).sum()
        dataframe["volume_usd_24h"] = vol_notional_24h.fillna(0.0).astype(np.float32)

        # 10. M?trica de Esticamento sobre a EMA21 (Filtro Anti-Exaust?o - Claude Fable 5.1)
        safe_atr = np.maximum(dataframe["atr"].fillna(1.0).values, 1e-4)
        dataframe["stretch_atr"] = ((close.values - dataframe["ema21"].values) / safe_atr).astype(np.float32)

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

        # 1. Filtro Minervini: ativo em est?gio 2 de expans?o saud?vel
        minervini_ok = (
            (close > dataframe["sma50"])
            & (dataframe["sma50"] > dataframe["sma150"])
            & (dataframe["sma150"] > dataframe["sma200"])
            & (dataframe["sma200_slope"] == 1)
        )

        # 2. Filtro Anti-Chasing: n?o comprar em rompimento esticado sem reteste
        chase_risk = (close > r1) & (low.shift(1) > r1)

        # 3. Pullback estrutural diferenciado por modo (V2 institucional exige maior rigor geom?trico)
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
            # V1 Equilibrada: toler?ncia padr?o (<= 1.5%)
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

        # 5. Momentum e Confirma??o de Retomada
        momentum_ok = (
            (dataframe["dual_bullish"] == 1)
            & (dataframe["dual_conflict"] == 0)
            & (dataframe["rsi"] <= active_rsi_max)
            & (dataframe["rsi"] > dataframe["rsi"].shift(1))
        )

        # 6. Filtro Kaufman Efficiency Ratio (Veto de Chop / Ru?do lateral)
        active_ker_min = self.KER_MIN_V2 if is_v2 else self.KER_MIN_V1
        ker_ok = dataframe["ker"] >= active_ker_min

        # 7. Filtro de Liquidez Notional 24h (Elimina slippage em livros rasos - Claude Fable 5.1)
        liquidity_ok = dataframe["volume_usd_24h"] >= self.min_24h_volume_usd

        # 8. Gatilho de Pullback na EMA21 e Anti-Esticamento (Valida??o Claude Fable 5.1)
        # O pre?o recua at? perto da EMA21 e sustenta suporte, evitando compras no topo esticado
        ema21_pullback = (low <= dataframe["ema21"] * 1.006) & (close >= dataframe["ema21"] * 0.992)
        not_stretched = dataframe["stretch_atr"] <= 1.2

        # Condi??o de Entrada Composta
        entry_conditions = (
            minervini_ok
            & (~chase_risk)
            & near_structural_support
            & ema21_pullback
            & not_stretched
            & regime_ok
            & momentum_ok
            & ker_ok
            & liquidity_ok
            & (dataframe["volume"] > 0)
        )

        dataframe.loc[entry_conditions, "enter_long"] = 1
        # Contrato imut?vel de entrada por trade (R1 Claude Fable 5.1):
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

        # Sa?da por revers?o conclu?da ? m?dia de Bollinger com RSI recuperado
        take_reversion = (close >= dataframe["bb_middle"]) & (dataframe["rsi"] > self.rsi_exit_mid)
        # Sa?da por sobrecompra extrema
        overbought_exit = dataframe["rsi"] > self.rsi_exit_extreme
        # Sa?da estrutural preventiva: perda da SMA50
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
        2. Trava de Clusters de Correla??o: limita no m?ximo 1 trade ativo por grupo de ativos correlacionados.
        """
        today = current_time.astimezone(UTC).date()
        total_balance = 1000.0
        if self.wallets is not None:
            try:
                total_balance = float(self.wallets.get_total_stake_amount())
            except Exception:
                pass

        # 0. Guarda Dura de Saldo Livre (Free Stake Buffer - Claude Fable 5.1)
        # Rejeita a entrada se o custo da ordem exceder 98% do saldo livre dispon?vel, prevenindo InsufficientFunds
        try:
            if self.wallets is not None:
                free_stake = float(self.wallets.get_available_stake_amount())
                order_cost = float(amount) * float(rate)
                buffer_ratio = 0.98
                limit = free_stake * buffer_ratio
                if order_cost > limit:
                    logger.warning(
                        "RenderQuant [FREE STAKE VETO]: Custo da ordem ($%.2f) excede limite de seguran?a ($%.2f / 98%% de $%.2f livre). Entrada em %s rejeitada.",
                        order_cost,
                        limit,
                        free_stake,
                        pair,
                    )
                    self._record_decision(pair, "REJECT", f"Saldo livre insuficiente (Custo ${order_cost:.2f} > Limite ${limit:.2f})")
                    return False
        except Exception as exc:
            logger.debug("RenderQuant: Falha ao verificar saldo livre dispon?vel: %s", exc)

        if self.current_day_utc != today or self.start_of_day_equity is None:
            self.current_day_utc = today
            self.start_of_day_equity = total_balance
            logger.info("RenderQuant: Novo dia UTC iniciado (%s). Equity base: $%.2f", today, total_balance)

        # 0. Filtro de Fric??o: Spread Guard Din?mico (Claude Fable 5.1)
        if self.dp is not None:
            try:
                ticker = self.dp.ticker(pair)
                if ticker and "bid" in ticker and "ask" in ticker and ticker["bid"] and ticker["bid"] > 0:
                    bid = float(ticker["bid"])
                    ask = float(ticker["ask"])
                    spread = (ask - bid) / bid
                    if spread > 0.0015:  # Veto se spread for superior a 0.15% (15 bps)
                        logger.info("RenderQuant [SPREAD VETO]: %s com spread excessivo de %.3f%% (> 0.150%%)", pair, spread * 100)
                        self._record_decision(pair, "REJECT", f"Spread excessivo no book ({spread*100:.3f}% > 0.150%)")
                        return False
            except Exception as exc:
                logger.debug("RenderQuant: Falha ao verificar spread de %s: %s", pair, exc)

        # 0.1 BTC Macro Regime Guard em Tempo Real (Claude Fable 5.1)
        stake = "USD"
        try:
            if hasattr(self, "config") and self.config:
                stake = self.config.get("stake_currency", "USD")
        except Exception:
            pass
        btc_pair = f"BTC/{stake}"
        if pair != btc_pair and self.dp is not None:
            try:
                btc_df, _ = self.dp.get_analyzed_dataframe(btc_pair, self.timeframe)
                if btc_df is not None and not btc_df.empty:
                    last_btc = btc_df.iloc[-1]
                    btc_c = float(last_btc.get("close", 0.0))
                    btc_e50 = float(last_btc.get("sma50", last_btc.get("ema50", 0.0)))
                    if btc_c > 0 and btc_e50 > 0 and btc_c < btc_e50:
                        logger.info("RenderQuant [BTC MACRO VETO]: %s bloqueado pois BTC ($%.0f) est? abaixo da m?dia de 50 ($%.0f)", pair, btc_c, btc_e50)
                        self._record_decision(pair, "REJECT", f"Regime Macro: BTC abaixo da m?dia de 50 ($ {btc_c:.0f} < $ {btc_e50:.0f})")
                        return False
            except Exception as exc:
                logger.debug("RenderQuant: Falha n?o cr?tica na checagem do BTC Macro Guard: %s", exc)

        # 1. Valida??o do Circuit Breaker Di?rio (C?lculo robusto sem exce??es silenciosas)
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
                        # Corre??o P0 SRE: usar a cota??o real do pr?prio trade aberto, jamais o rate do par avaliado!
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
                    "RenderQuant [CIRCUIT BREAKER]: Perda di?ria de %.2f%% atingiu limite de -%.2f%%. Entrada em %s bloqueada.",
                    daily_dd_pct * 100,
                    self.daily_loss_stop_pct * 100,
                    pair,
                )
                self._record_decision(pair, "REJECT", f"Circuit Breaker ativo (Drawdown di?rio: {daily_dd_pct*100:.2f}%)")
                return False
        except Exception as exc:
            logger.debug("RenderQuant: Falha n?o cr?tica na checagem do Daily Loss: %s", exc)

        # 2. Valida??o de Clusters de Correla??o
        try:
            open_trades = Trade.get_trades_proxy(is_open=True)
            active_pairs = [t.pair for t in open_trades if getattr(t, "is_open", True)]

            for cluster_name, cluster_pairs in self.CORRELATION_CLUSTERS.items():
                if pair in cluster_pairs:
                    active_in_cluster = sum(1 for p in active_pairs if p in cluster_pairs)
                    if active_in_cluster >= self.MAX_TRADES_PER_CLUSTER:
                        logger.info(
                            "RenderQuant [CLUSTER VETO]: Cluster %s saturado (%d posi??es ativas). Entrada em %s vetada.",
                            cluster_name,
                            active_in_cluster,
                            pair,
                        )
                        self._record_decision(pair, "REJECT", f"Cluster {cluster_name} saturado ({active_in_cluster} posi??es ativas)", cluster=cluster_name)
                        return False
        except Exception as exc:
            logger.debug("RenderQuant: Falha n?o cr?tica na valida??o de clusters: %s", exc)

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
                self._record_decision(pair, "REJECT", f"Portfolio Heat excederia teto (Projetado: {portfolio_heat*100:.2f}% > {self.portfolio_heat_limit*100:.2f}%)", heat=portfolio_heat)
                return False

            logger.info(
                "RenderQuant [PORTFOLIO HEAT PASS]: Heat projetado %.2f%% / %.2f%% (Equity $%.2f). Entrada em %s autorizada.",
                portfolio_heat * 100,
                self.portfolio_heat_limit * 100,
                total_balance,
                pair,
            )
            self._record_decision(pair, "ENTER", "Filtros de momentum, KER, saldos e risco aprovados", heat=portfolio_heat)
        except Exception as exc:
            logger.error(
                "RenderQuant [FAIL-CLOSED RISK]: Erro na avalia??o de Portfolio Heat: %s. Entrada em %s vetada por seguran?a.",
                exc,
                pair,
            )
            self._record_decision(pair, "REJECT", f"Falha na avalia??o de risco/heat: {exc}")
            return False

        # 4. Gatekeeper Cognitivo AI Guardian (Mirai API / Claude Fable 5.1 - Claude Fable 5.1)
        try:
            from user_data.ai_guardian import consult_ai_guardian, get_state
            ai_state = get_state()
            if ai_state.get("enabled", False):
                ker_val = 0.40
                atr_val = 0.018
                vol_z_val = 1.0
                regime_bull_val = True
                if self.dp is not None:
                    dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
                    if dataframe is not None and not dataframe.empty:
                        last_row = dataframe.iloc[-1]
                        ker_val = float(last_row.get("ker", 0.40))
                        atr_val = float(last_row.get("atr_pct", 0.018))
                        vol_z_val = float(last_row.get("vol_modified_z", 1.0))
                        regime_bull_val = bool(last_row.get("market_regime_bull", True))

                verdict = consult_ai_guardian(
                    pair=pair,
                    rate=rate,
                    ker=ker_val,
                    atr_pct=atr_val,
                    vol_z=vol_z_val,
                    regime_bull=regime_bull_val,
                    daily_drawdown_pct=daily_dd_pct if 'daily_dd_pct' in locals() else 0.0,
                    mode=self._cached_strategy_mode,
                )

                if not verdict.get("aprovado", True):
                    motivo_ia = verdict.get("motivo", "Veto emitido pela IA")
                    logger.info("RenderQuant [AI GUARDIAN VETO]: Entrada em %s vetada pela IA: %s", pair, motivo_ia)
                    self._record_decision(pair, "REJECT", f"IA Guardian: {motivo_ia}")
                    return False

                logger.info(
                    "RenderQuant [AI GUARDIAN PASS]: Entrada em %s aprovada pela IA (Convic??o: %.0f%% | %s)",
                    pair,
                    verdict.get("conviccao", 0.70) * 100,
                    verdict.get("motivo"),
                )
        except Exception as exc:
            logger.debug("RenderQuant: Falha n?o cr?tica na consulta ao AI Guardian: %s", exc)

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
        """Fase 1 (ml4t/skills): Volatility-Targeted Inverse Position Sizing.

        Stake = risk_budget / stressed_dist_pct, onde a dist?ncia de stop ?
        proporcional ? volatilidade realizada (ATR) do par, com custos reais
        de corretora e slippage. Equaliza o risco em d?lares de cada aposta.
        Preserva travas institucionais: regime lateral, Quarter-Kelly (N >= 200) e cap de carteira.
        """
        mode = self._get_active_strategy_mode()
        is_v2 = (mode == "RenderQuantV2_Institutional")

        # Par?metros de risco calibrados conforme o modo ativo
        active_risk_pct = 0.0075 if is_v2 else self.max_risk_pct_per_trade  # 0.75% vs 1.0%
        active_stake_ratio = 0.08 if is_v2 else self.max_stake_ratio        # 8% vs 10%
        lateral_multiplier = 0.65 if is_v2 else 0.75                       # -35% vs -25% em range

        # 0. Wallet total (fallback defensivo)
        total_wallet = max(float(max_stake), float(proposed_stake), 0.0)
        if self.wallets is not None:
            try:
                total_wallet = float(self.wallets.get_total_stake_amount())
            except Exception:
                pass

        # 1. Volatilidade realizada (ml4t/skills) -> dist?ncia de stop proporcional ao ru?do real do ativo
        atr_pct = 0.018  # fallback robusto (1.8%)
        if self.dp is not None:
            try:
                dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
                if dataframe is not None and not dataframe.empty and "atr_pct" in dataframe.columns:
                    raw_atr = float(dataframe["atr_pct"].iloc[-1])
                    if np.isfinite(raw_atr) and raw_atr > 0:
                        atr_pct = raw_atr
            except Exception:
                pass

        vol_stop_pct = float(np.clip(atr_pct * 2.2, 0.020, 0.060))

        # 2. Custos reais: taker na entrada + taker na sa?da + slippage
        stressed_dist_pct = vol_stop_pct + (self.taker_fee_pct * 2.0) + (self.slip_floor_bps / 10000.0)

        # 3. Sizing inverso (ml4t): or?amento de risco / dist?ncia estressada
        risk_budget = total_wallet * active_risk_pct
        if stressed_dist_pct > 0:
            calculated_stake = risk_budget / stressed_dist_pct
        else:
            calculated_stake = proposed_stake

        # 4a. Multiplicador por Regime de Mercado (lateral/bear reduz exposi??o - Maverick)
        if self.dp is not None:
            try:
                dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
                if dataframe is not None and not dataframe.empty:
                    is_bull = bool(dataframe["market_regime_bull"].iloc[-1])
                    if not is_bull:
                        calculated_stake *= lateral_multiplier
            except Exception:
                pass

        # 4b. Quarter-Kelly estritamente para N >= 200 trades (Diretriz P0 Claude Fable 5.1 / HyperGrok)
        try:
            closed_trades = Trade.get_trades_proxy(is_open=False)
            profits = [
                float(t.close_profit)
                for t in closed_trades
                if getattr(t, "close_profit", None) is not None
            ]
            if len(profits) >= 200:
                wins = [p for p in profits if p > 0]
                losses = [p for p in profits if p <= 0]
                if wins and losses:
                    p_win = len(wins) / len(profits)
                    avg_win = sum(wins) / len(wins)
                    avg_loss = abs(sum(losses) / len(losses))
                    if avg_loss > 0:
                        b = avg_win / avg_loss
                        kelly_f = p_win - (1.0 - p_win) / b
                        quarter_kelly = max(0.0, kelly_f) * 0.25
                        if quarter_kelly > 0:
                            calculated_stake = min(
                                calculated_stake,
                                total_wallet * quarter_kelly,
                            )
        except Exception:
            pass

        # 4c. Cap m?ximo de carteira (8% V2 / 10% V1)
        cap = min(calculated_stake, total_wallet * active_stake_ratio, float(max_stake))

        # 4d. Trava de min_stake da corretora
        if min_stake is not None and cap < float(min_stake):
            logger.warning(
                "RenderQuant [MIN_STAKE VETO]: Ordem em %s descartada ? stake calculado ($%.2f) abaixo do m?nimo da corretora ($%.2f).",
                pair,
                cap,
                float(min_stake),
            )
            return 0.0

        if not np.isfinite(cap) or cap <= 0:
            return 0.0

        return round(float(cap), 8)

    # Custo Kraken spot round-trip (0.26% * 2 + 0.10% spread = 0.62%)
    ROUND_TRIP_COST = 0.0062

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
        """Trailing Stop Escalonado com Breakeven P?s-Taxas (Tr?ade de Ouro - Claude Fable 5.1)."""
        atr_pct = 0.015
        if self.dp is not None:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is not None and not dataframe.empty and "atr_pct" in dataframe.columns:
                val = float(dataframe["atr_pct"].iloc[-1])
                if np.isfinite(val) and val > 0:
                    atr_pct = val

        dynamic_stop = -float(np.clip(1.8 * atr_pct, 0.020, 0.045))

        # Degrau 1: Lucro atingiu +2.5% -> Breakeven garantido cobrindo taxas (+0.60% l?quido)
        if current_profit >= 0.025 and current_profit < 0.050:
            return max(dynamic_stop, 0.0060)

        # Degrau 2: Lucro atingiu +5.0% -> Trava lucro l?quido de +3.00%
        if current_profit >= 0.050 and current_profit < 0.080:
            return max(dynamic_stop, 0.0300)

        # Degrau 3: Lucro atingiu +8.0% -> Ratchet din?mico com ATR garantindo pelo menos +5.0% l?quido
        if current_profit >= 0.080:
            trailing_dist = max(atr_pct * 1.5, 0.020)
            desired_stop = current_profit - trailing_dist
            return max(dynamic_stop, desired_stop, 0.0500)

        return dynamic_stop

    # Cache de rate-limit para chamadas de sa?da da IA (m?x 1 consulta a cada 15m por par)
    _last_ai_exit_check: dict[str, float] = {}

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs: Any,
    ) -> Optional[Union[str, bool]]:
        """
        Sa?da Cognitiva Inteligente com Claude Fable 5.1 (Mirai API).
        Avalia encerramento antecipado para embolsar lucros ou cortar estagna??o antes de revers?o.
        Possui rate-limit estrito de 15 minutos por par para lat?ncia zero e custo controlado.
        """
        trade_open_date = getattr(trade, "open_date_utc", None)
        open_minutes = 0.0
        if trade_open_date:
            try:
                # normaliza datetime com timezone se necess?rio
                t_diff = (current_time - trade_open_date).total_seconds()
                open_minutes = max(0.0, t_diff / 60.0)
            except Exception:
                pass

        qualifies_profit_shield = (current_profit >= 0.012)
        qualifies_stagnation_cut = (open_minutes >= 90.0 and current_profit < 0.005)

        if not (qualifies_profit_shield or qualifies_stagnation_cut):
            return None

        # Rate limit rigoroso: no m?ximo 1 consulta a cada 900 segundos (15 minutos) por par
        now_ts = time.time()
        last_check = self._last_ai_exit_check.get(pair, 0.0)
        if (now_ts - last_check) < 900.0:
            return None

        # Coleta dados t?cnicos atuais do par
        ker_val = 0.40
        atr_val = 0.018
        regime_bull_val = True
        if self.dp is not None:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is not None and not dataframe.empty:
                last_row = dataframe.iloc[-1]
                ker_val = float(last_row.get("ker", 0.40))
                atr_val = float(last_row.get("atr_pct", 0.018))
                regime_bull_val = bool(last_row.get("market_regime_bull", True))

        self._last_ai_exit_check[pair] = now_ts

        try:
            from user_data.ai_guardian import consult_ai_exit
            verdict = consult_ai_exit(
                pair=pair,
                current_profit_pct=current_profit,
                open_duration_min=open_minutes,
                ker=ker_val,
                atr_pct=atr_val,
                regime_bull=regime_bull_val,
            )
            if verdict.get("sair", False):
                reason = verdict.get("motivo", "IA Profit Shield")
                logger.info(
                    "RenderQuant [AI GUARDIAN EXIT]: Encerrando %s (Lucro: %+.2f%% | Dura??o: %.0fm) -> %s",
                    pair, current_profit * 100, open_minutes, reason
                )
                return f"ia_fable_{reason[:25].strip()}"
        except Exception as exc:
            logger.debug("RenderQuant: Erro n?o cr?tico no custom_exit da IA: %s", exc)

        return None

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
