"""RenderQuantV4_Adaptive - Estratégia Quantitativa Multi-Regime Institucional (4h).

Inspirada no cruzamento de dados de mercado, NostalgiaForInfinity e SMAOffsetProtect:
1. Multi-Timeframe Context: BTC Macro Guard em 4h (não compra altcoins se BTC estiver sob crash).
2. Mean Reversion com Confirmação de Pavio (Wick Rejection): elimina compras em faca caindo.
3. Stoploss Dinâmico com Breakeven Automático:
   - Trava de Breakeven (+0.6% cobrindo taxas da Kraken) assim que o trade atinge +2.5% de lucro.
   - Trailing Stop dinâmico de 2.0% quando o lucro ultrapassa +5.0% para surfar ralis parabólicos.
4. Paridade de Risco e Suporte a Micro-Bancas (Kraken ordermin adaptativo).
5. Proteções Nativas Freqtrade: CooldownPeriod, StoplossGuard e MaxDrawdown.
"""
import logging
from datetime import datetime
from typing import Any, Optional, Union

import numpy as np
import talib.abstract as ta
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from pandas import DataFrame

logger = logging.getLogger(__name__)


class RenderQuantV4_Adaptive(IStrategy):
    INTERFACE_VERSION = 3
    timeframe: str = "4h"
    startup_candle_count: int = 220
    process_only_new_candles: bool = True
    can_short: bool = False

    # Stop Loss de segurança nativo na Kraken
    stoploss: float = -0.05
    use_custom_stoploss: bool = True
    minimal_roi = {
        "0": 0.15,      # ROI amplo para deixar o trailing stop conduzir o trade
        "1440": 0.08,   # 8% após 24h
        "2880": 0.04,   # 4% após 48h
    }

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

    @property
    def protections(self):
        return [
            {
                "method": "CooldownPeriod",
                "stop_duration_candles": 6,  # 24h de cooldown no mesmo par
            },
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 18,
                "trade_limit": 2,
                "stop_duration_candles": 12,
                "only_per_pair": True,
            },
            {
                "method": "MaxDrawdown",
                "calculation_mode": "equity",
                "lookback_period_candles": 84,
                "trade_limit": 8,
                "stop_duration_candles": 12,
                "max_allowed_drawdown": 0.10,
            },
        ]

    # Parâmetros Quantitativos Calibrados
    bb_pos_entry: float = 0.08          # Fundo relativo da banda inferior (0 a 8%)
    ema200_floor: float = 0.90          # Piso estrutural: acima de 90% da EMA200
    bb_pos_exit: float = 0.80           # Saída no topo da banda de Bollinger
    rsi_exit: int = 70                  # Saída por sobrecompra extrema
    min_vol_usd_24h: float = 3_000_000.0# Mínimo $3M USD negociados em 24h
    min_wick_ratio: float = 0.18        # Pavio inferior mínimo (rejeição de preço)

    def informative_pairs(self):
        """Monitora BTC em 4h como termômetro macro do mercado cripto."""
        stake = "USDT"
        if hasattr(self, "config") and self.config:
            stake = self.config.get("stake_currency", "USDT")
        return [(f"BTC/{stake}", self.timeframe)]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if dataframe.empty:
            return dataframe
        close = dataframe["close"]
        high = dataframe["high"]
        low = dataframe["low"]
        open_p = dataframe["open"]

        # Médias estruturais
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)

        # Volatilidade e Momentum
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = (dataframe["atr"] / close).fillna(0.02)

        # Bandas de Bollinger (20, 2.0)
        bb = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0, matype=0)
        dataframe["bb_lo"] = bb["lowerband"]
        dataframe["bb_mid"] = bb["middleband"]
        dataframe["bb_hi"] = bb["upperband"]
        width = (dataframe["bb_hi"] - dataframe["bb_lo"]).replace(0, np.nan)
        dataframe["bb_pos"] = ((close - dataframe["bb_lo"]) / width).clip(-1, 2).fillna(1.5)

        # Pavio de rejeição inferior (Wick Rejection)
        candle_body_bottom = np.minimum(open_p, close)
        candle_range = (high - low).replace(0, np.nan)
        dataframe["lower_wick_ratio"] = ((candle_body_bottom - low) / candle_range).fillna(0.0)

        # Volume notional acumulado em 24h (6 velas de 4h)
        dataframe["vol_usd_24h"] = (dataframe["volume"] * close).rolling(6, min_periods=3).sum().fillna(0)

        # BTC Macro Guard (se o par não for o próprio BTC)
        stake = "USDT"
        if hasattr(self, "config") and self.config:
            stake = self.config.get("stake_currency", "USDT")
        btc_pair = f"BTC/{stake}"
        if metadata.get("pair") != btc_pair and self.dp is not None:
            try:
                btc_df = self.dp.get_pair_dataframe(btc_pair, self.timeframe)
                if btc_df is not None and not btc_df.empty:
                    btc_close = btc_df["close"]
                    btc_ema50 = ta.EMA(btc_df, timeperiod=50)
                    btc_change_4h = btc_close.pct_change()
                    dataframe["btc_above_ema50"] = (btc_close > btc_ema50).reindex(dataframe.index, method="ffill").fillna(True)
                    dataframe["btc_no_crash"] = (btc_change_4h > -0.04).reindex(dataframe.index, method="ffill").fillna(True)
                else:
                    dataframe["btc_above_ema50"] = True
                    dataframe["btc_no_crash"] = True
            except Exception:
                dataframe["btc_above_ema50"] = True
                dataframe["btc_no_crash"] = True
        else:
            dataframe["btc_above_ema50"] = True
            dataframe["btc_no_crash"] = True

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if dataframe.empty:
            return dataframe

        # Condição de entrada de alta convicção
        cond = (
            # 1. Preço na extremidade inferior da Banda de Bollinger
            (dataframe["bb_pos"] < self.bb_pos_entry)
            # 2. Piso de segurança macro (evita colapso estrutural abaixo de 90% da EMA200)
            & (dataframe["close"] > dataframe["ema200"] * self.ema200_floor)
            # 3. Confirmação de rejeição de fundo (pavio inferior expressivo OU candle verde)
            & ((dataframe["lower_wick_ratio"] >= self.min_wick_ratio) | (dataframe["close"] > dataframe["open"]))
            # 4. BTC Macro Guard (não compra se BTC estiver despencando)
            & (dataframe["btc_no_crash"] == True)
            # 5. Filtro de liquidez mínima em 24h
            & (dataframe["vol_usd_24h"] >= self.min_vol_usd_24h)
            & (dataframe["volume"] > 0)
        )

        dataframe.loc[cond, "enter_long"] = 1
        dataframe.loc[cond, "enter_tag"] = "V4:adaptive_reversion"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if dataframe.empty:
            return dataframe

        # Saída regular por regressão à média superior ou sobrecompra de momentum
        cond = (
            ((dataframe["bb_pos"] > self.bb_pos_exit) | (dataframe["rsi"] > self.rsi_exit))
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[cond, "exit_long"] = 1
        return dataframe

    def custom_stoploss(
        self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
        current_profit: float, after_fill: bool, **kwargs: Any,
    ) -> Optional[float]:
        """Stop-Loss Dinâmico com Breakeven Garantido e Trailing Stop."""
        # 1. Se lucro atingir +2.5%: move stop para +0.6% (Breakeven com taxas cobertas)
        if 0.025 <= current_profit < 0.050:
            # Trava o stop 0.6% acima da entrada (lucro líquido mínimo garantido)
            return -(current_profit - 0.006)

        # 2. Se lucro atingir +5.0% ou mais: trailing stop de 2.0% para capturar super-tendências
        if current_profit >= 0.050:
            return -0.020

        # Caso contrário, mantém o stoploss inicial de -5.0%
        return -0.050

    def custom_stake_amount(
        self, pair: str, current_time: datetime, current_rate: float,
        proposed_stake: float, min_stake: Optional[float], max_stake: float,
        leverage: float, entry_tag: Optional[str], side: str, **kwargs: Any,
    ) -> float:
        """Risk parity institucional com fallback seguro para a ordem mínima da Kraken."""
        total_wallet = max(float(max_stake), float(proposed_stake), 0.0)
        if self.wallets is not None:
            try:
                total_wallet = float(self.wallets.get_total_stake_amount())
            except Exception:
                pass
        stop_d = 0.05 + 0.0062
        stake = min((total_wallet * 0.01) / stop_d, total_wallet * 0.10, float(max_stake))
        if min_stake is not None and stake < float(min_stake):
            if float(min_stake) <= total_wallet:
                stake = float(min_stake)
            else:
                return 0.0
        if not np.isfinite(stake) or stake <= 0:
            return 0.0
        return round(float(stake), 8)

    def confirm_trade_entry(
        self, pair: str, order_type: str, amount: float, rate: float,
        time_in_force: str, current_time: datetime, entry_tag: Optional[str],
        side: str, **kwargs: Any,
    ) -> bool:
        """Gate de risco fail-closed: circuit breaker diário -2.5% e heat cap 3-6%."""
        try:
            today_start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
            closed = Trade.get_trades_proxy(is_open=False)
            realized = sum(
                float(t.close_profit_abs or 0.0)
                for t in closed if getattr(t, "close_date_utc", None) and t.close_date_utc >= today_start
            )
            open_tr = Trade.get_trades_proxy(is_open=True)
            unreal = 0.0
            for t in open_tr:
                r = getattr(t, "current_rate", None) or getattr(t, "open_rate", None)
                if r and hasattr(t, "calc_profit_abs"):
                    unreal += float(t.calc_profit_abs(r))
            total_balance = 1000.0
            if self.wallets is not None:
                total_balance = float(self.wallets.get_total_stake_amount())
            daily_dd = (realized + unreal) / max(total_balance, 10.0)
            if daily_dd <= -0.025:
                logger.warning("V4 [CIRCUIT BREAKER]: dd diário %.2f%% - entrada %s vetada", daily_dd * 100, pair)
                return False

            open_heat = 0.0
            for t in open_tr:
                sl = getattr(t, "stop_loss_or_limit", None) or (t.open_rate or 0) * 0.95
                open_heat += abs((t.open_rate or 0) - sl) * float(getattr(t, "amount", 0) or 0)
            incoming = 0.05 * float(amount) * float(rate)
            max_heat = 0.06 if total_balance < 50.0 else 0.03
            if (open_heat + incoming) / max(total_balance, 10.0) > max_heat:
                logger.warning("V4 [HEAT VETO]: heat projetado acima de %.2f%% - %s vetada", max_heat * 100, pair)
                return False
        except Exception as exc:
            logger.error("V4 [FAIL-CLOSED]: erro no gate de risco (%s) - %s vetada", exc, pair)
            return False
        return True
