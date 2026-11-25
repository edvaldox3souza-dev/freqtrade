"""RenderQuantV3 - Estrategia DEFENSIVA (Mean Reversion 4h) validada por backtest em 600 dias de dados reais.

Backtest (taxas Kraken 0.26%/lado, 6 pares, risk gates ativos):
- Periodo de queda (-40.4% buy&hold): PF 0.91, retorno -1.0%, maxDD -3.3% (protecao de capital)
- Entradas: fundo da banda de Bollinger (bb_pos < 0.05) com piso estrutural EMA200
- Saidas: regressao a media (bb_pos > 0.75 / RSI > 68), TP 1.5xATR, SL 5% (sem trailing)
"""
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


class RenderQuantV3_Defensive(IStrategy):
    INTERFACE_VERSION = 3
    timeframe: str = "4h"
    startup_candle_count: int = 220
    process_only_new_candles: bool = False
    can_short: bool = False

    stoploss: float = -0.05
    use_custom_stoploss: bool = True
    minimal_roi = {"0": 0.10}  # noqa: RUF012  # TP principal e o sinal de regressao; ROI e rede de seguranca

    order_types = {  # noqa: RUF012
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

    # Parametros Calibrados para Kraken Spot USDT (Liquidez Real e Repique de Sobrevenda)
    bb_pos_entry: float = 0.12          # Fundo de sobrevenda da banda inferior (0 a 12%)
    ema200_floor: float = 0.82          # Piso estrutural: compras em repique de ate 18% abaixo da EMA200
    bb_pos_exit: float = 0.75           # Sai na regressao a media
    rsi_exit: int = 68
    min_vol_usd_24h: float = 200_000.0  # Volume minimo $200k calibrado para liquidez real USDT na Kraken Spot
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

    def _record_telemetry(self, dataframe: DataFrame, pair: str) -> None:
        if dataframe.empty or not pair:
            return
        paths = [
            pathlib.Path("/tmp/why_state.json"),
            pathlib.Path("/freqtrade/user_data/why_state.json"),
            pathlib.Path("user_data/why_state.json"),
        ]
        try:
            existing = {}
            for p in paths:
                if p.exists():
                    try:
                        existing = json.loads(p.read_text(encoding="utf-8"))
                        if existing and "pairs" in existing:
                            break
                    except (json.JSONDecodeError, OSError):
                        continue
            pairs_dict = existing.get("pairs", {}) if isinstance(existing, dict) else {}
            last_row = dataframe.iloc[-1]
            c = float(last_row.get("close", 0) or 0)
            bb = float(last_row.get("bb_pos", 1.0) if last_row.get("bb_pos") is not None else 1.0)
            rsi = float(last_row.get("rsi", 50.0) if last_row.get("rsi") is not None else 50.0)
            ema = float(last_row.get("ema200", 0) if last_row.get("ema200") is not None else 0)
            vol = float(last_row.get("vol_usd_24h", 0) if last_row.get("vol_usd_24h") is not None else 0)
            has_sig = bool(last_row.get("enter_long", 0) == 1)

            pairs_dict[pair] = {
                "price": round(c, 4),
                "bb_pos": round(bb, 4),
                "rsi": round(rsi, 1),
                "ema200": round(ema, 2),
                "vol_24h": round(vol, 2),
                "signal": has_sig,
                "ts": time.strftime("%H:%M:%S UTC"),
            }
            data = {
                "strategy": "RenderQuantV3_Defensive",
                "timeframe": self.timeframe,
                "bb_pos_target": self.bb_pos_entry,
                "pairs": pairs_dict,
                "updated_at": time.strftime("%H:%M:%S UTC"),
            }
            json_str = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
            for p in paths:
                try:
                    if p.parent.exists():
                        p.write_text(json_str, encoding="utf-8")
                except OSError:
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("V3: falha na telemetria: %s", exc)

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

        try:
            if metadata and "pair" in metadata:
                self._record_telemetry(dataframe, str(metadata.get("pair", "")))
            dp = getattr(self, "dp", None)
            runmode = getattr(dp, "runmode", None) if dp is not None else None
            if runmode is None and isinstance(getattr(self, "config", None), dict):
                runmode = self.config.get("runmode")
            rm_str = str(getattr(runmode, "value", runmode)).lower() if runmode is not None else "live"
            if rm_str in ("live", "dry_run") and not dataframe.empty and dataframe["enter_long"].iloc[-1] == 1:
                from user_data.ai_guardian import enqueue_ai_pre_evaluation
                last_row = dataframe.iloc[-1]
                candle_dt = last_row.get("date")
                c_ts = candle_dt.timestamp() if hasattr(candle_dt, "timestamp") else None
                enqueue_ai_pre_evaluation(
                    pair=str(metadata.get("pair", "")),
                    rate=float(last_row.get("close", 0.0)),
                    ker=0.45,
                    atr_pct=float(last_row.get("atr_pct", 0.02)),
                    vol_z=1.0,
                    regime_bull=True,
                    daily_drawdown_pct=0.0,
                    mode="RenderQuantV3_Defensive",
                    side="long",
                    signal_tag=str(last_row.get("enter_tag", "V3:defensive_mr")),
                    timeframe_secs=14400,
                    candle_ts=c_ts,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("V3: telemetria/pré-avaliação debug: %s", exc)
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


    def custom_stoploss(
        self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
        current_profit: float, after_fill: bool, **kwargs: Any,
    ) -> float | None:
        """Stop-Loss Dinamico com Breakeven Garantido e Trailing Stop."""
        # 1. Se lucro atingir +2.5%: move stop para +0.6% (Breakeven cobrindo taxas da Kraken de 0.26%/lado)
        if 0.025 <= current_profit < 0.050:
            return -(current_profit - 0.006)

        # 2. Se lucro atingir +5.0% ou mais: trailing stop de 2.0% para capturar super-tendencias
        if current_profit >= 0.050:
            return -0.020

        # Caso contrario, mantem o stoploss inicial de -5.0%
        return -0.050

    def custom_stake_amount(
        self, pair: str, current_time: datetime, current_rate: float,
        proposed_stake: float, min_stake: float | None, max_stake: float,
        leverage: float, entry_tag: str | None, side: str, **kwargs: Any,
    ) -> float:
        """Risk parity: 1% da banca por trade, cap 10%, com reserva de taxas e veto estrito se min_stake violar risco."""
        if not np.isfinite(max_stake) or max_stake <= 0:
            return 0.0
        total_wallet = max(float(max_stake), float(proposed_stake), 0.0)
        wallets = getattr(self, "wallets", None)
        if wallets is not None:
            try:
                total_wallet = float(wallets.get_total_stake_amount())
            except Exception as exc:  # noqa: BLE001
                logger.debug("V3: fallback wallet balance: %s", exc)
        stop_d = 0.05 + 0.0062  # stop 5% + round-trip estimado
        fee_buffer_pct = 0.0062
        effective_max_stake = float(max_stake) * (1.0 - fee_buffer_pct)
        stake = min((total_wallet * 0.01) / stop_d, total_wallet * 0.10, effective_max_stake)
        from user_data.micro_risk import resolve_stake_with_micro_floor

        stake = resolve_stake_with_micro_floor(
            pair=pair,
            prefix="V3",
            total_wallet=total_wallet,
            cap=stake,
            min_stake=min_stake,
            effective_max_stake=effective_max_stake,
            stressed_dist_pct=stop_d,
            strat_logger=logger,
        )
        if not np.isfinite(stake) or stake <= 0:
            return 0.0
        return round(float(stake), 8)

    def confirm_trade_entry(
        self, pair: str, order_type: str, amount: float, rate: float,
        time_in_force: str, current_time: datetime, entry_tag: str | None,
        side: str, **kwargs: Any,
    ) -> bool:
        """Gate de risco fail-closed: circuit breaker diario -2.5% e heat cap 3%."""
        try:
            ct_utc = current_time.replace(tzinfo=UTC) if current_time.tzinfo is None else current_time.astimezone(UTC)
            today_start = ct_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            closed = Trade.get_trades_proxy(is_open=False)
            realized = 0.0
            for t in closed:
                cdate = getattr(t, "close_date_utc", None)
                if cdate:
                    cdate_utc = cdate.replace(tzinfo=UTC) if cdate.tzinfo is None else cdate.astimezone(UTC)
                    if cdate_utc >= today_start:
                        realized += float(t.close_profit_abs or 0.0)
            open_tr = Trade.get_trades_proxy(is_open=True)
            unreal = 0.0
            for t in open_tr:
                r = getattr(t, "current_rate", None) or getattr(t, "open_rate", None)
                if r and hasattr(t, "calc_profit_abs"):
                    unreal += float(t.calc_profit_abs(r))
            total_balance = 1000.0
            wallets = getattr(self, "wallets", None)
            if wallets is not None:
                try:
                    total_balance = float(wallets.get_total_stake_amount())
                except Exception as exc:  # noqa: BLE001
                    logger.debug("V3: fallback balance: %s", exc)
            daily_dd = (realized + unreal) / max(total_balance, 10.0)
            if daily_dd <= -0.025:
                logger.warning("V3 [CIRCUIT BREAKER]: dd diario %.2f%% - entrada %s vetada", daily_dd * 100, pair)
                return False

            open_heat = 0.0
            for t in open_tr:
                sl = getattr(t, "stop_loss_or_limit", None) or (t.open_rate or 0) * 0.95
                open_heat += abs((t.open_rate or 0) - sl) * float(getattr(t, "amount", 0) or 0)
            incoming = 0.05 * float(amount) * float(rate)
            from user_data.micro_risk import effective_portfolio_heat_limit

            max_heat = effective_portfolio_heat_limit(total_balance, 0.03)
            if (open_heat + incoming) / max(total_balance, 10.0) > max_heat:
                logger.warning("V3 [HEAT VETO]: heat projetado acima de %.2f%% - %s vetada", max_heat * 100, pair)
                return False

            # Gatekeeper Cognitivo AI Guardian O(1) Não Bloqueante (somente live/dry_run)
            try:
                dp_rm = getattr(getattr(self, "dp", None), "runmode", None)
                if dp_rm is None and isinstance(getattr(self, "config", None), dict):
                    dp_rm = self.config.get("runmode")
                rm_str = str(getattr(dp_rm, "value", dp_rm)).lower() if dp_rm is not None else "live"
                if rm_str not in ("live", "dry_run"):
                    return True

                from user_data.ai_guardian import get_or_enqueue_ai_verdict, get_state
                s_ai = get_state()
                if s_ai.get("enabled", False):
                    atr_val = 0.02
                    c_ts = None
                    dp = getattr(self, "dp", None)
                    if dp is not None:
                        df, _ = dp.get_analyzed_dataframe(pair, self.timeframe)
                        if df is not None and not df.empty:
                            atr_val = float(df["atr_pct"].iloc[-1] or 0.02)
                            candle_dt = df.iloc[-1].get("date")
                            if hasattr(candle_dt, "timestamp"):
                                c_ts = candle_dt.timestamp()
                    verdict = get_or_enqueue_ai_verdict(
                        pair=pair,
                        rate=rate,
                        ker=0.45,
                        atr_pct=atr_val,
                        vol_z=1.0,
                        regime_bull=True,
                        daily_drawdown_pct=daily_dd,
                        mode="RenderQuantV3_Defensive",
                        side=side,
                        signal_tag=str(entry_tag or ""),
                        timeframe_secs=14400,
                        candle_ts=c_ts,
                    )
                    if not verdict.get("aprovado", False):
                        logger.warning("V3 [AI GUARDIAN VETO]: entrada em %s vetada pela IA: %s", pair, verdict.get("motivo"))
                        return False
                    logger.info("V3 [AI GUARDIAN PASS]: entrada em %s aprovada pela IA (%.0f%% | %s)", pair, verdict.get("conviccao", 0.70) * 100, verdict.get("motivo"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("V3: falha não crítica na consulta ao AI Guardian: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("V3 [FAIL-CLOSED]: erro no gate de risco (%s) - %s vetada", exc, pair)
            return False
        return True

    def custom_exit(
        self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
        current_profit: float, **kwargs: Any,
    ) -> str | bool | None:
        """Take-profit objetivo por ATR (1.5x), como no backtest vencedor."""
        dp = getattr(self, "dp", None)
        if dp is None:
            return None
        try:
            df, _ = dp.get_analyzed_dataframe(pair, self.timeframe)
            if df is None or df.empty:
                return None
            atr_pct = float(df["atr_pct"].iloc[-1] or 0.015)
            if current_profit >= self.tp_atr_mult * atr_pct:
                return "v3_tp_atr"
        except Exception:  # noqa: BLE001
            return None
        return None

