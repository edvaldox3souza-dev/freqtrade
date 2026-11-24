"""
Shadow Tracker — Rastreamento de Sinais Simulados (Forward Telemetry) no Neon PostgreSQL.

Permite que o robô opere em Conta Real na Kraken enquanto simula e audita
paralelamente no banco Neon todos os sinais gerados e vetados pelo Claude Opus 5.5,
medindo a eficácia matemática das estratégias e a taxa de acerto dos vetos da IA.
"""
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("shadow_tracker")
_SHADOW_LOCK = threading.RLock()


def _get_neon_dsn() -> Optional[str]:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url or "postgres" not in url:
        return None
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url.replace("postgresql+psycopg2://", "postgresql://")


def _get_connection():
    import psycopg2
    dsn = _get_neon_dsn()
    if not dsn:
        return None
    return psycopg2.connect(dsn, connect_timeout=8)


def ensure_shadow_tables() -> bool:
    """Cria a tabela de shadow trading no Neon DB caso não exista."""
    try:
        conn = _get_connection()
        if not conn:
            return False
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS shadow_trades (
                        id SERIAL PRIMARY KEY,
                        pair TEXT NOT NULL,
                        strategy TEXT NOT NULL,
                        entry_rate NUMERIC NOT NULL,
                        entry_tag TEXT,
                        ai_approved BOOLEAN,
                        ai_conviccao NUMERIC,
                        ai_motivo TEXT,
                        ai_model TEXT,
                        target_rate NUMERIC NOT NULL,
                        stop_rate NUMERIC NOT NULL,
                        highest_rate NUMERIC,
                        lowest_rate NUMERIC,
                        exit_rate NUMERIC,
                        profit_pct NUMERIC,
                        status TEXT NOT NULL DEFAULT 'OPEN',
                        opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        closed_at TIMESTAMPTZ,
                        notes TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_shadow_status ON shadow_trades(status);
                    CREATE INDEX IF NOT EXISTS idx_shadow_pair ON shadow_trades(pair);
                    """
                )
        conn.close()
        return True
    except Exception as exc:
        logger.debug("shadow_tracker ensure_shadow_tables falhou: %s", exc)
        return False


def record_shadow_signal(
    pair: str,
    strategy: str,
    entry_rate: float,
    entry_tag: str = "",
    ai_approved: bool = True,
    ai_conviccao: float = 0.70,
    ai_motivo: str = "",
    ai_model: str = "claude-opus-5.5",
    target_pct: float = 0.035,  # Alvo médio ponderado de +3.5%
    stop_pct: float = -0.050,   # Stop loss de -5.0%
) -> Optional[int]:
    """Registra uma oportunidade de trade sombra no Neon Postgres."""
    with _SHADOW_LOCK:
        try:
            conn = _get_connection()
            if not conn:
                return None
            target_rate = round(entry_rate * (1.0 + target_pct), 8)
            stop_rate = round(entry_rate * (1.0 + stop_pct), 8)

            with conn:
                with conn.cursor() as cur:
                    # Anti-duplicação: não cria outro shadow trade no mesmo par se houver um aberto criado nos últimos 60 min
                    cur.execute(
                        """
                        SELECT id FROM shadow_trades 
                        WHERE pair = %s AND status = 'OPEN' AND opened_at >= now() - INTERVAL '60 minutes'
                        LIMIT 1;
                        """,
                        (pair,),
                    )
                    existing = cur.fetchone()
                    if existing:
                        conn.close()
                        return existing[0]

                    cur.execute(
                        """
                        INSERT INTO shadow_trades (
                            pair, strategy, entry_rate, entry_tag,
                            ai_approved, ai_conviccao, ai_motivo, ai_model,
                            target_rate, stop_rate, highest_rate, lowest_rate,
                            status, opened_at
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            'OPEN', now()
                        ) RETURNING id;
                        """,
                        (
                            pair, strategy, entry_rate, entry_tag,
                            ai_approved, ai_conviccao, ai_motivo, ai_model,
                            target_rate, stop_rate, entry_rate, entry_rate,
                        ),
                    )
                    row = cur.fetchone()
                    new_id = row[0] if row else None
            conn.close()
            logger.info("Shadow trade #%s registrado em %s (Taxa: %s | AI: %s)", new_id, pair, entry_rate, ai_approved)
            return new_id
        except Exception as exc:
            logger.debug("shadow_tracker record_shadow_signal falhou: %s", exc)
            return None


def record_shadow_signal_async(**kwargs: Any) -> None:
    """Dispara o registro de shadow trade no Neon DB em background sem bloquear confirm_trade_entry."""
    threading.Thread(
        target=record_shadow_signal,
        kwargs=kwargs,
        daemon=True,
        name="shadow-record",
    ).start()


def _kraken_symbol_map(pair: str) -> str:
    cleaned = pair.replace("/", "").upper()
    if cleaned.startswith("BTC"):
        return cleaned.replace("BTC", "XBT")
    return cleaned


def fetch_kraken_rates(pairs: List[str]) -> Dict[str, float]:
    """Busca cotações em lote diretamente na API pública da Kraken."""
    import json
    import urllib.request
    if not pairs:
        return {}
    mapped = [_kraken_symbol_map(p) for p in pairs]
    query = ",".join(mapped)
    url = f"https://api.kraken.com/0/public/Ticker?pair={query}"
    if not url.startswith("https://api.kraken.com/"):
        return {}
    res = {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Freqtrade-ShadowTracker/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
            result = data.get("result", {})
            for p, m in zip(pairs, mapped):
                for k, v in result.items():
                    if k.upper() == m or k.upper().endswith(m):
                        c_arr = v.get("c", [])
                        if c_arr:
                            res[p] = float(c_arr[0])
                            break
    except Exception as exc:
        logger.debug("fetch_kraken_rates warn: %s", exc)
    return res


def update_open_shadow_trades(get_rate_fn: Callable[[str], Optional[float]]) -> int:
    """
    Varre os shadow trades abertos, atualiza máximas/mínimas e fecha se atingir target ou stop.
    Retorna o número de trades finalizados nesta rodada.
    """
    with _SHADOW_LOCK:
        closed_count = 0
        try:
            conn = _get_connection()
            if not conn:
                return 0

            open_trades = []
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, pair, entry_rate, target_rate, stop_rate, highest_rate, lowest_rate, opened_at
                    FROM shadow_trades
                    WHERE status = 'OPEN';
                    """
                )
                open_trades = cur.fetchall()

            if not open_trades:
                conn.close()
                return 0

            with conn:
                with conn.cursor() as cur:
                    for tid, pair, entry, target, stop, highest, lowest, opened_at in open_trades:
                        try:
                            current_rate = get_rate_fn(pair)
                            if not current_rate or current_rate <= 0:
                                continue

                            entry_f = float(entry)
                            target_f = float(target)
                            stop_f = float(stop)
                            high_f = max(float(highest or entry_f), current_rate)
                            low_f = min(float(lowest or entry_f), current_rate)

                            # 1. Checagem de Take Profit
                            if current_rate >= target_f:
                                profit_pct = (target_f - entry_f) / entry_f
                                cur.execute(
                                    """
                                    UPDATE shadow_trades
                                    SET status = 'CLOSED_PROFIT', exit_rate = %s, profit_pct = %s,
                                        highest_rate = %s, lowest_rate = %s, closed_at = now()
                                    WHERE id = %s;
                                    """,
                                    (target_f, profit_pct, high_f, low_f, tid),
                                )
                                closed_count += 1
                                logger.info("Shadow trade #%s (%s) fechou no LUCRO (+%.2f%%)", tid, pair, profit_pct * 100)

                            # 2. Checagem de Stop Loss
                            elif current_rate <= stop_f:
                                profit_pct = (stop_f - entry_f) / entry_f
                                cur.execute(
                                    """
                                    UPDATE shadow_trades
                                    SET status = 'CLOSED_STOPLOSS', exit_rate = %s, profit_pct = %s,
                                        highest_rate = %s, lowest_rate = %s, closed_at = now()
                                    WHERE id = %s;
                                    """,
                                    (stop_f, profit_pct, high_f, low_f, tid),
                                )
                                closed_count += 1
                                logger.info("Shadow trade #%s (%s) fechou no STOP (%.2f%%)", tid, pair, profit_pct * 100)

                            # 3. Atualiza apenas a excursão máxima/mínima enquanto aberto
                            else:
                                cur.execute(
                                    """
                                    UPDATE shadow_trades
                                    SET highest_rate = %s, lowest_rate = %s
                                    WHERE id = %s;
                                    """,
                                    (high_f, low_f, tid),
                                )
                        except Exception as row_exc:
                            logger.debug("Erro ao atualizar shadow trade #%s: %s", tid, row_exc)

            conn.close()
            return closed_count
        except Exception as exc:
            logger.debug("shadow_tracker update_open_shadow_trades falhou: %s", exc)
            return 0


def sync_all_shadow_trades() -> int:
    """Sincroniza todos os shadow trades abertos consultando as cotações da Kraken."""
    rates_cache: Dict[str, float] = {}

    def _resolver(pair: str) -> Optional[float]:
        nonlocal rates_cache
        if not rates_cache:
            # Busca todos os pares spot monitorados em uma única chamada
            pairs_to_query = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT", "LINK/USDT", pair]
            rates_cache = fetch_kraken_rates(list(set(pairs_to_query)))
        return rates_cache.get(pair)

    return update_open_shadow_trades(_resolver)


def get_shadow_stats() -> Dict[str, Any]:
    """Retorna métricas analíticas consolidadas de Shadow Trading do Neon DB."""
    last_trades: List[Dict[str, Any]] = []
    total_signals = 0
    open_signals = 0
    closed_profit = 0
    closed_stop = 0
    win_rate = 0.0
    cum_profit_pct = 0.0
    ai_vetoes_total = 0
    ai_vetoes_saved_loss = 0
    ai_veto_success_rate = 0.0

    def _snapshot() -> Dict[str, Any]:
        return {
            "total_signals": total_signals,
            "open_signals": open_signals,
            "closed_profit": closed_profit,
            "closed_stop": closed_stop,
            "win_rate": win_rate,
            "cum_profit_pct": cum_profit_pct,
            "ai_vetoes_total": ai_vetoes_total,
            "ai_vetoes_saved_loss": ai_vetoes_saved_loss,
            "ai_veto_success_rate": ai_veto_success_rate,
            "last_trades": last_trades,
        }

    try:
        conn = _get_connection()
        if not conn:
            return _snapshot()

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM shadow_trades;")
            total_signals = int(cur.fetchone()[0] or 0)

            cur.execute("SELECT count(*) FROM shadow_trades WHERE status = 'OPEN';")
            open_signals = int(cur.fetchone()[0] or 0)

            cur.execute("SELECT count(*) FROM shadow_trades WHERE status = 'CLOSED_PROFIT';")
            closed_profit = int(cur.fetchone()[0] or 0)

            cur.execute("SELECT count(*) FROM shadow_trades WHERE status = 'CLOSED_STOPLOSS';")
            closed_stop = int(cur.fetchone()[0] or 0)

            closed_total = closed_profit + closed_stop
            if closed_total > 0:
                win_rate = round((closed_profit / closed_total) * 100.0, 1)

            cur.execute("SELECT coalesce(sum(profit_pct), 0) FROM shadow_trades WHERE status LIKE 'CLOSED%';")
            cum_profit_pct = round(float(cur.fetchone()[0] or 0.0) * 100.0, 2)

            # Eficácia dos Vetos da IA (Quantos trades vetados teriam tomado Stop-Loss)
            cur.execute("SELECT count(*) FROM shadow_trades WHERE ai_approved = false;")
            ai_vetoes_total = int(cur.fetchone()[0] or 0)

            cur.execute("SELECT count(*) FROM shadow_trades WHERE ai_approved = false AND status = 'CLOSED_STOPLOSS';")
            ai_vetoes_saved_loss = int(cur.fetchone()[0] or 0)

            if ai_vetoes_total > 0:
                ai_veto_success_rate = round((ai_vetoes_saved_loss / ai_vetoes_total) * 100.0, 1)

            # Últimos 5 trades registrados
            cur.execute(
                """
                SELECT id, pair, strategy, entry_rate, status, profit_pct, ai_approved, ai_conviccao, opened_at
                FROM shadow_trades
                ORDER BY id DESC LIMIT 5;
                """
            )
            rows = cur.fetchall()
            for r in rows:
                last_trades.append({
                    "id": r[0],
                    "pair": r[1],
                    "strategy": r[2],
                    "entry_rate": float(r[3]),
                    "status": r[4],
                    "profit_pct": float(r[5]) if r[5] is not None else None,
                    "ai_approved": bool(r[6]),
                    "ai_conviccao": float(r[7] or 0.0),
                    "opened_at": r[8].strftime("%d/%m %H:%M") if r[8] else "",
                })

        conn.close()
        return _snapshot()
    except Exception as exc:
        logger.debug("shadow_tracker get_shadow_stats falhou: %s", exc)
        return _snapshot()


def format_shadow_stats_text() -> str:
    """Formata o relatório de telemetria para o Telegram."""
    st = get_shadow_stats()
    ts = time.strftime("%H:%M:%S UTC")

    lines = [
        "👻 *Telemetria de Shadow Trading (Neon PostgreSQL)*\n",
        f"• *Sinais Monitorados:* `{st['total_signals']}` (`{st['open_signals']}` ativos)",
        f"• *Acertos (Take Profit):* `{st['closed_profit']}` ✅",
        f"• *Stops Disparados:* `{st['closed_stop']}` 🛑",
        f"• *Taxa de Acerto Simulada:* `{st['win_rate']}%`",
        f"• *Lucro Simulado Acumulado:* `{st['cum_profit_pct']:+.2f}%`",
        "",
        "🧠 *Eficácia do Guardião IA (Claude Opus 5.5):*",
        f"• *Total de Vetos da IA:* `{st['ai_vetoes_total']}`",
        f"• *Prejuízos Evitados pela IA:* `{st['ai_vetoes_saved_loss']}`",
        f"• *Precisão de Veto:* `{st['ai_veto_success_rate']}%`",
        "",
        f"🕒 *Atualizado às:* `{ts}`",
        "\n_Os dados acima são simulados paralelamente em tempo real no banco Neon para refinamento contínuo das estratégias sem risco ao capital real._",
    ]
    return "\n".join(lines)
