import logging
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy

logger = logging.getLogger(__name__)


class RenderQuantV1(IStrategy):
    """Estratégia Quantitativa Institucional para Render (V2 Unificada).

    Integração de alta performance:
    - Maverick MCP: Minervini Trend Template (Stage 2) + Multiplicador por Regime de Mercado
    - Trading Skills: Modified Z-Score (MAD) de Volume + Anti-Chasing Pivots + Dual Crossover Anti-Whipsaw
    - HyperGrok Trading Desk: Stressed Stop Sizing + Daily Loss Circuit Breaker + Trava de Clusters de Correlação
    """

    INTERFACE_VERSION = 3

    can_short: bool = False
    timeframe: str = "15m"
    startup_candle_count: int = 240
    process_only_new_candles: bool = True

    # Realização escalonada no tempo
    minimal_roi = {
        "0": 0.045,
        "60": 0.025,
        "180": 0.012,
    }

    # Stoploss inicial de segurança na corretora
    stoploss: float = -0.04
    stoploss_on_exchange: bool = True
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
    taker_fee_pct: float = 0.0006          # 0.06% de taxa taker da exchange
    slip_floor_bps: float = 10.0           # 10 bps de derrapagem mínima estimada no stop
    kelly_fraction: float = 0.25           # Quarter-Kelly
    max_stake_ratio: float = 0.10          # Teto máximo de 10% da carteira por trade
    cold_start_stake_ratio: float = 0.05   # 5% nos primeiros trades

    # Clusters de Correlação (HyperGrok) - Evita risco direcional duplicado
    CORRELATION_CLUSTERS = {
        "MAJORS": ["BTC/USD", "ETH/USD", "BTC/USDT", "ETH/USDT"],
        "LAYER1": ["SOL/USD", "ADA/USD", "AVAX/USD", "NEAR/USD", "SUI/USD", "SOL/USDT", "ADA/USDT"],
        "DEFI_INFRA": ["LINK/USD", "DOT/USD", "LINK/USDT", "DOT/USDT"],
    }
    MAX_TRADES_PER_CLUSTER = 1

    current_day_utc = None
    start_of_day_equity = None

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

        # 3. Modified Z-Score de Volume via MAD (Trading Skills)
        rolling_vol = vol.rolling(window=72)
        med_vol = rolling_vol.median()
        abs_dev = (vol - med_vol).abs()
        mad_vol = abs_dev.rolling(window=72).median().replace(0, 1e-6)
        dataframe["vol_modified_z"] = (0.6745 * (vol - med_vol) / mad_vol).astype(np.float32)
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

        # 6. Métrica de ATR percentual para o stoploss
        dataframe["atr_pct"] = np.where(
            close > 0,
            (dataframe["atr"] / close).astype(np.float32),
            0.015,
        )

        # 7. Regime de Mercado (Maverick)
        dataframe["market_regime_bull"] = (dataframe["sma50"] > dataframe["sma150"]).astype(np.int8)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if dataframe.empty:
            return dataframe

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

        # 3. Pullback estrutural: próximo ao pivot central P, ao suporte S1 ou à banda inferior de Bollinger
        near_structural_support = (
            ((close - p).abs() / p <= 0.015)
            | ((close - s1).abs() / s1 <= 0.015)
            | (close <= dataframe["bb_lower"] * 1.006)
        )

        # 4. Regime e Volatilidade
        regime_ok = (
            (dataframe["natr"] >= self.min_natr)
            & (dataframe["natr"] <= self.max_natr)
            & (dataframe["adx"] >= self.min_adx)
        )

        # 5. Momentum e Confirmação de Retomada
        momentum_ok = (
            (dataframe["dual_bullish"] == 1)
            & (dataframe["dual_conflict"] == 0)
            & (dataframe["rsi"] <= self.rsi_entry_max)
            & (dataframe["rsi"] > dataframe["rsi"].shift(1))
        )

        # Condição de Entrada Composta
        entry_conditions = (
            minervini_ok
            & (~chase_risk)
            & near_structural_support
            & regime_ok
            & momentum_ok
            & (dataframe["volume"] > 0)
        )

        dataframe.loc[entry_conditions, "enter_long"] = 1
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
        entry_tag: Optional[str],
        side: str,
        **kwargs: Any,
    ) -> bool:
        """Gatekeeper Institucional (HyperGrok Trading Desk).

        1. Daily Loss Circuit Breaker: bloqueia novas compras se a carteira cair mais de 2.5% no dia UTC.
        2. Trava de Clusters de Correlação: limita no máximo 1 trade ativo por grupo de ativos correlacionados.
        """
        today = current_time.astimezone(timezone.utc).date()
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

        # 1. Validação do Circuit Breaker Diário
        try:
            today_start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
            closed_trades = Trade.get_trades_proxy(is_open=False)
            closed_today = [
                t for t in closed_trades
                if getattr(t, "close_date_utc", None) and t.close_date_utc >= today_start
            ]
            realized_pnl = sum(float(getattr(t, "close_profit_abs", 0.0) or 0.0) for t in closed_today)

            open_trades = Trade.get_trades_proxy(is_open=True)
            unrealized_pnl = sum(
                float(getattr(t, "realized_profit", 0.0) or 0.0) +
                float(getattr(t, "calc_profit_abs", lambda r: 0.0)(rate) or 0.0)
                for t in open_trades
            )
            total_daily_pnl = realized_pnl + unrealized_pnl
            daily_dd_pct = total_daily_pnl / max(self.start_of_day_equity, 10.0)

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

        return True

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: Optional[float],
        max_stake: float,
        leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs: Any,
    ) -> float:
        """Stressed Sizing + Quarter-Kelly + Multiplicador de Regime."""
        total_wallet = max(float(max_stake), float(proposed_stake), 0.0)
        if self.wallets is not None:
            try:
                total_wallet = float(self.wallets.get_total_stake_amount())
            except Exception:
                pass

        # 1. Stressed Stop Distance (HyperGrok)
        nominal_stop_rate = current_rate * (1.0 - abs(self.stoploss))
        slip_usd = current_rate * (self.slip_floor_bps / 10000.0)
        stressed_stop = nominal_stop_rate - slip_usd
        taker_fees = (current_rate + stressed_stop) * self.taker_fee_pct
        stressed_dist = abs(current_rate - stressed_stop) + taker_fees

        risk_budget = total_wallet * self.max_risk_pct_per_trade
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
                    calculated_stake *= 0.75  # Reduz 25% da exposição em regime lateral

        # 3. Ajuste por Quarter-Kelly
        try:
            closed_trades = Trade.get_trades_proxy(is_open=False)
            profits = [
                float(t.close_profit)
                for t in closed_trades
                if getattr(t, "close_profit", None) is not None
            ]
            if len(profits) >= 10:
                wins = [p for p in profits if p > 0]
                losses = [abs(p) for p in profits if p < 0]
                if wins and losses:
                    win_rate = len(wins) / len(profits)
                    payoff = (sum(wins) / len(wins)) / (sum(losses) / len(losses))
                    full_kelly = win_rate - ((1.0 - win_rate) / payoff)
                    quarter_kelly = max(0.0, full_kelly * self.kelly_fraction)
                    kelly_cap = total_wallet * min(self.max_stake_ratio, max(0.02, quarter_kelly))
                    calculated_stake = min(calculated_stake, kelly_cap)
        except Exception:
            pass

        cap = min(calculated_stake, total_wallet * self.max_stake_ratio, float(max_stake))
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
        """Stoploss Dinâmico baseado em 2x ATR + Ratchet Progressivo Contínuo."""
        atr_pct = 0.015
        if self.dp is not None:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is not None and not dataframe.empty and "atr_pct" in dataframe.columns:
                val = float(dataframe["atr_pct"].iloc[-1])
                if np.isfinite(val) and val > 0:
                    atr_pct = val

        dynamic_stop = -float(np.clip(2.0 * atr_pct, 0.015, 0.040))

        # Trailing Ratchet contínuo (Trading Skills & HyperGrok)
        if current_profit >= 0.030:
            return max(dynamic_stop, -0.008)  # Lucro protegido
        if current_profit >= 0.015:
            return max(dynamic_stop, -0.005)  # Breakeven líquido
        return dynamic_stop
