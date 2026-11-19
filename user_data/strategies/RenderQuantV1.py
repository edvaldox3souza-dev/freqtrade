import logging
from datetime import datetime
from typing import Any, Optional

import numpy as np
import pandas as pd
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy

logger = logging.getLogger(__name__)


class RenderQuantV1(IStrategy):
    """Estratégia quantitativa enxuta para Render Free Tier (512 MB RAM / 0.1 vCPU).

    Combina:
    - Filtro de regime e volatilidade (ADX + NATR via TA-Lib)
    - Entrada por reversão à média curta alinhada à tendência (EMA + Bollinger + RSI)
    - Stoploss dinâmico baseado em ATR + Proteção nativa MaxDrawdown
    - Dimensionamento de posição por Quarter-Kelly limitado a 10% da banca
    """

    INTERFACE_VERSION = 3

    can_short: bool = False
    timeframe: str = "15m"
    startup_candle_count: int = 100
    process_only_new_candles: bool = True

    minimal_roi = {
        "0": 0.045,
        "60": 0.025,
        "180": 0.012,
    }

    stoploss: float = -0.04
    use_custom_stoploss: bool = True

    use_exit_signal: bool = True
    exit_profit_only: bool = False
    ignore_roi_if_entry_signal: bool = False

    # Parâmetros quantitativos fixos (evita overfitting em produção)
    min_natr: float = 0.25
    max_natr: float = 3.50
    min_adx: float = 18.0
    rsi_entry_max: float = 38.0
    rsi_exit_mid: float = 62.0
    rsi_exit_extreme: float = 72.0

    # Gestão de capital (Quarter-Kelly com teto rígido de 10%)
    kelly_fraction: float = 0.25
    max_stake_ratio: float = 0.10
    cold_start_stake_ratio: float = 0.05

    @property
    def protections(self) -> list[dict[str, Any]]:
        return [
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 96,
                "trade_limit": 3,
                "stop_duration_candles": 24,
                "max_allowed_drawdown": 0.10,
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
        # Indicadores principais 100% vetorizados via TA-Lib
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["natr"] = ta.NATR(dataframe, timeperiod=14)

        bollinger = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0, matype=0)
        dataframe["bb_lower"] = bollinger["lowerband"]
        dataframe["bb_middle"] = bollinger["middleband"]

        # Colunas auxiliares intermediárias (removidas antes do return para poupar RAM)
        dataframe["_ema_fast"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["_ema_slow"] = ta.EMA(dataframe, timeperiod=55)
        dataframe["_atr_pct"] = np.where(
            dataframe["close"] > 0,
            dataframe["atr"] / dataframe["close"],
            0.0,
        )

        dataframe["ema_trend_ok"] = (dataframe["_ema_fast"] > dataframe["_ema_slow"]).astype(np.int8)
        dataframe["atr_pct"] = dataframe["_atr_pct"].astype(np.float32)

        # Libera explicitamente colunas intermediárias antes de retornar
        dataframe.drop(
            columns=["_ema_fast", "_ema_slow", "_atr_pct"],
            inplace=True,
            errors="ignore",
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if dataframe.empty:
            return dataframe
        regime_ok = (
            (dataframe["natr"] >= self.min_natr)
            & (dataframe["natr"] <= self.max_natr)
            & (dataframe["adx"] >= self.min_adx)
        )
        momentum_reversion_ok = (
            (dataframe["ema_trend_ok"] == 1)
            & (dataframe["close"] <= dataframe["bb_lower"] * 1.004)
            & (dataframe["rsi"] < self.rsi_entry_max)
            & (dataframe["rsi"] > dataframe["rsi"].shift(1))
        )
        dataframe.loc[
            regime_ok & momentum_reversion_ok & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if dataframe.empty:
            return dataframe
        take_reversion = (dataframe["close"] >= dataframe["bb_middle"]) & (
            dataframe["rsi"] > self.rsi_exit_mid
        )
        overbought_exit = dataframe["rsi"] > self.rsi_exit_extreme
        dataframe.loc[
            (take_reversion | overbought_exit) & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1
        return dataframe

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
        atr_pct = 0.015
        if self.dp is not None:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is not None and not dataframe.empty and "atr_pct" in dataframe.columns:
                val = float(dataframe["atr_pct"].iloc[-1])
                if np.isfinite(val) and val > 0:
                    atr_pct = val

        # Stop dinâmico leve baseado em 2x ATR, limitado entre -1.5% e -4.0%
        dynamic_stop = -float(np.clip(2.0 * atr_pct, 0.015, 0.040))

        # Trailing leve para proteger ganhos abertos sem sobrecarregar CPU
        if current_profit >= 0.030:
            return max(dynamic_stop, -0.008)
        if current_profit >= 0.015:
            return max(dynamic_stop, -0.012)
        return dynamic_stop

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
        total_wallet = max(float(max_stake), float(proposed_stake), 0.0)
        if self.wallets is not None:
            try:
                total_wallet = float(self.wallets.get_total_stake_amount())
            except Exception:
                pass

        stake_ratio = self.cold_start_stake_ratio
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
                    stake_ratio = min(self.max_stake_ratio, max(0.02, quarter_kelly))
        except Exception:
            stake_ratio = self.cold_start_stake_ratio

        cap = min(total_wallet * min(stake_ratio, self.max_stake_ratio), float(max_stake))
        if min_stake is not None and cap < float(min_stake):
            logger.warning(
                "RenderQuantV1: Ordem ignorada em %s — stake calculado (%.4f) abaixo do min_stake da corretora (%.4f).",
                pair,
                cap,
                float(min_stake),
            )
            return 0.0
        return round(float(cap), 8)
