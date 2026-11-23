"""RenderQuantV3 - Estrategia DEFENSIVA (Mean Reversion 4h) validada por backtest em 600 dias de dados reais.

Backtest (taxas Kraken 0.26%/lado, 6 pares, risk gates ativos):
- Periodo de queda (-40.4% buy&hold): PF 0.91, retorno -1.0%, maxDD -3.3% (protecao de capital)
- Entradas: fundo da banda de Bollinger (bb_pos < 0.05) com piso estrutural EMA200
- Saidas: regressao a media (bb_pos > 0.75 / RSI > 68), TP 1.5xATR, SL 5% (sem trailing)
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


class RenderQuantV3_Defensive(IStrategy):
    INTERFACE_VERSION = 3
    timeframe: str = "4h"
    startup_candle_count: int = 220
    process_only_new_candles: bool = True
    can_short: bool = False

    stoploss: float = -0.05
    use_custom_stoploss: bool = False
    minimal_roi = {"0": 0.10}  # TP principal e o sinal de regressao; ROI e rede de seguranca

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

    # Prote??es nativas Freqtrade (sem lookahead, validadas em backtest com --enable-protections)
    @property
    def protections(self):
        return [
            {  # nao re-entra no mesmo par por 6 velas (24h) apos sair
                "method": "CooldownPeriod",
                "stop_duration_candles": 6,
            },
            {  # para o par por 12 velas (48h) se 2 stoploss nos ultimos 3 dias
                "method": "StoplossGuard",
                "lookback_period_candles": 18,
                "trade_limit": 2,
                "stop_duration_candles": 12,
                "only_per_pair": True,
            },
            {  # trava global se drawdown de 10% em 14 dias com 8+ trades
                "method": "MaxDrawdown",
                "calculation_mode": "equity",
                "lookback_period_candles": 84,
                "trade_limit": 8,
                "stop_duration_candles": 12,
                "max_allowed_drawdown": 0.10,
            },
        ]

    # Parametros do backtest vencedor
    bb_pos_entry: float = 0.05      # so compra no fundo da banda inferior
    ema200_floor: float = 0.92      # piso estrutural: nunca comprar abaixo de 92% da EMA200
    bb_pos_exit: float = 0.75       # sai na regressao a media
    rsi_exit: int = 68
    min_vol_usd_24h: float = 5_000_000.0
    tp_atr_mult: float = 1.5

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if dataframe.empty:
            return dataframe
        close = dataframe["close"]

        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = (dataframe["atr"] / close).fillna(0.015)

        bb = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0, matype=0)
        dataframe["bb_lo"] = bb["lowerband"]
        dataframe["bb_mid"] = bb["middleband"]
        dataframe["bb_hi"] = bb["upperband"]
        width = (dataframe["bb_hi"] - dataframe["bb_lo"]).replace(0, np.nan)
        dataframe["bb_pos"] = ((close - dataframe["bb_lo"]) / width).clip(-1, 2).fillna(1.5)

        dataframe["vol_usd_24h"] = (dataframe["volume"] * close).rolling(6, min_periods=3).sum().fillna(0)
        dataframe["tp_ref"] = close * (1 + self.tp_atr_mult * dataframe["atr_pct"])
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if dataframe.empty:
            return dataframe
        cond = (
            (dataframe["bb_pos"] < self.bb_pos_entry)
            & (dataframe["close"] > dataframe["ema200"] * self.ema200_floor)
            & (dataframe["vol_usd_24h"] >= self.min_vol_usd_24h)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[cond, "enter_long"] = 1
        dataframe.loc[cond, "enter_tag"] = "V3:defensive_mr"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if dataframe.empty:
            return dataframe
        cond = (
            ((dataframe["bb_pos"] > self.bb_pos_exit) | (dataframe["rsi"] > self.rsi_exit))
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[cond, "exit_long"] = 1
        return dataframe


    def custom_stake_amount(
        self, pair: str, current_time: datetime, current_rate: float,
        proposed_stake: float, min_stake: Optional[float], max_stake: float,
        leverage: float, entry_tag: Optional[str], side: str, **kwargs: Any,
    ) -> float:
        """Risk parity: 1% da banca por trade, cap 10% (identico ao backtest vencedor), com fallback seguro para ordermin."""
        total_wallet = max(float(max_stake), float(proposed_stake), 0.0)
        if self.wallets is not None:
            try:
                total_wallet = float(self.wallets.get_total_stake_amount())
            except Exception:
                pass
        stop_d = 0.05 + 0.0062  # stop 5% + round-trip estimado
        stake = min((total_wallet * 0.01) / stop_d, total_wallet * 0.10, float(max_stake))
        if min_stake is not None and stake < float(min_stake):
            # Se a carteira cobre a ordem mínima da corretora, permite stake = min_stake
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
        """Gate de risco fail-closed: circuit breaker diario -2.5% e heat cap 3%."""
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
                logger.warning("V3 [CIRCUIT BREAKER]: dd diario %.2f%% - entrada %s vetada", daily_dd * 100, pair)
                return False

            open_heat = 0.0
            for t in open_tr:
                sl = getattr(t, "stop_loss_or_limit", None) or (t.open_rate or 0) * 0.95
                open_heat += abs((t.open_rate or 0) - sl) * float(getattr(t, "amount", 0) or 0)
            incoming = 0.05 * float(amount) * float(rate)
            # Para bancas institucionais (> $50), heat cap rigoroso de 3%.
            # Para micro-bancas (< $50), heat cap de 6% para acomodar 1 posicao minima de exchange.
            max_heat = 0.06 if total_balance < 50.0 else 0.03
            if (open_heat + incoming) / max(total_balance, 10.0) > max_heat:
                logger.warning("V3 [HEAT VETO]: heat projetado acima de %.2f%% - %s vetada", max_heat * 100, pair)
                return False

            # Gatekeeper Cognitivo AI Guardian (Claude Fable via Mirai API)
            try:
                from user_data.ai_guardian import consult_ai_guardian, get_state
                s_ai = get_state()
                if s_ai.get("enabled", False):
                    atr_val = 0.02
                    dp = getattr(self, "dp", None)
                    if dp is not None:
                        df, _ = dp.get_analyzed_dataframe(pair, self.timeframe)
                        if df is not None and not df.empty:
                            atr_val = float(df["atr_pct"].iloc[-1] or 0.02)
                    verdict = consult_ai_guardian(
                        pair=pair,
                        rate=rate,
                        ker=0.45,
                        atr_pct=atr_val,
                        vol_z=1.0,
                        regime_bull=True,
                        daily_drawdown_pct=daily_dd,
                        mode="RenderQuantV3_Defensive",
                    )
                    if not verdict.get("aprovado", True):
                        logger.warning("V3 [AI GUARDIAN VETO]: entrada em %s vetada pela IA: %s", pair, verdict.get("motivo"))
                        return False
                    logger.info("V3 [AI GUARDIAN PASS]: entrada em %s aprovada pela IA (%.0f%% | %s)", pair, verdict.get("conviccao", 0.70) * 100, verdict.get("motivo"))
            except Exception as exc:
                logger.debug("V3: falha não crítica na consulta ao AI Guardian: %s", exc)
        except Exception as exc:
            logger.error("V3 [FAIL-CLOSED]: erro no gate de risco (%s) - %s vetada", exc, pair)
            return False
        return True

    def custom_exit(
        self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
        current_profit: float, **kwargs: Any,
    ) -> Optional[Union[str, bool]]:
        """Take-profit objetivo por ATR (1.5x), como no backtest vencedor."""
        if self.dp is None:
            return None
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if df is None or df.empty:
                return None
            atr_pct = float(df["atr_pct"].iloc[-1] or 0.015)
            if current_profit >= self.tp_atr_mult * atr_pct:
                return "v3_tp_atr"
        except Exception:
            return None
        return None

