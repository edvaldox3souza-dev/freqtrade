"""
AI Guardian — Camada de Veto e Convicção com Claude Fable 5.1 (Mirai API) para Freqtrade.
Permite ligar/desligar, editar chaves, ajustar convicção e testar conexão 100% pelo Telegram.
"""
import atexit
import json
import logging
import os
import pathlib
import queue
import re
import threading
import time

import requests

from user_data.state_store import load_state, save_state

logger = logging.getLogger("ai_guardian")

STATE_DB_KEY = "ai_guardian_state"

# Localização do arquivo de estado em disco (persistência robusta no container e no host)
STATE_PATHS = [
    pathlib.Path("/freqtrade/user_data/ai_guardian_state.json"),
    pathlib.Path("user_data/ai_guardian_state.json"),
]

DEFAULT_STATE = {
    "enabled": True,
    "api_key": os.environ.get("AI_GUARDIAN_API_KEY", "").strip(),
    "endpoint": "https://api.miraiapi.com/v1/chat/completions",
    "model": os.environ.get("AI_GUARDIAN_MODEL", "claude-opus-5.5").strip() or "claude-opus-5.5",
    "min_conviccao": 0.65,
    "last_verdict": None,
    "total_queries": 0,
    "total_vetoes": 0,
    "total_approvals": 0,
    "total_ai_exits": 0,
    "enable_ai_exits": True,
}

_STATE_LOCK = threading.RLock()


def _get_target_path() -> pathlib.Path:
    for p in STATE_PATHS:
        try:
            if p.parent.exists():
                return p
        except OSError as exc:
            logger.debug("[AI GUARDIAN] Path check failed for %s: %s", p, exc)
    return STATE_PATHS[1]


def get_state() -> dict:
    with _STATE_LOCK:
        st = load_state(STATE_DB_KEY, STATE_PATHS, DEFAULT_STATE)
        env_key = os.environ.get("AI_GUARDIAN_API_KEY", "").strip()
        if env_key:
            st["api_key"] = env_key
        env_model = os.environ.get("AI_GUARDIAN_MODEL", "").strip()
        if env_model:
            st["model"] = env_model
        elif st.get("model") in ("claude-fable-5.1", "claude-fable-5"):
            st["model"] = "claude-opus-5.5"
        return st


def update_state(**kwargs) -> dict:
    with _STATE_LOCK:
        current = get_state()
        for k, v in kwargs.items():
            current[k] = v
        save_state(STATE_DB_KEY, current, STATE_PATHS)
        return current


def mask_key(k: str) -> str:
    if not k:
        return "NÃO CONFIGURADA"
    if len(k) <= 8:
        return "********"
    return k[:5] + "..." + k[-4:]


# Cache thread-safe de veredictos por vela e sinal + Worker Pool Assíncrono
# Chave: (pair, mode, side, candle_bucket, signal_tag)
# Garante:
# 1. Leituras em confirm_trade_entry() são O(1) sem chamadas HTTP ou espera por workers
# 2. Alinhamento estrito à vela do timeframe (padrão 900s = 15m): nunca atravessa virada de vela
# 3. Diferencia par, estratégia (mode), lado (side) e sinal (signal_tag)
# 4. Respostas fail_closed NUNCA são cacheadas (permite recuperação imediata da API)
# 5. Pool limitado de workers, fila limitada, rejeição de respostas atrasadas e métricas
_CACHE_LOCK = threading.RLock()
_ENTRY_VERDICT_CACHE: dict[tuple[str, str, str, int, str], tuple[float, dict]] = {}
_IN_FLIGHT_EVENTS: dict[tuple[str, str, str, int, str], threading.Event] = {}
_MAX_CACHE_SIZE = 256
_DEFAULT_TIMEFRAME_SECS = 900

_MAX_WORKERS = 2
_MAX_QUEUE_SIZE = 16
_TASK_QUEUE: queue.Queue = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
_QUEUED_OR_IN_FLIGHT: set[tuple[str, str, str, int, str]] = set()
_WORKER_THREADS: list[threading.Thread] = []
_STOP_EVENT = threading.Event()
_WORKER_EPOCH = 0

_METRICS: dict[str, float | int] = {
    "cache_hits": 0,
    "cache_misses": 0,
    "enqueued": 0,
    "deduplicated": 0,
    "queue_dropped": 0,
    "worker_errors": 0,
    "late_discarded": 0,
    "completed": 0,
    "total_http_latency_ms": 0.0,
}


def configure_ai_worker(max_workers: int = 2, max_queue_size: int = 16) -> None:
    """Reconfigura limites de workers e fila (reinicia o pool com segurança)."""
    global _MAX_WORKERS, _MAX_QUEUE_SIZE, _TASK_QUEUE
    shutdown_ai_worker(wait=True, timeout=2.0)
    with _CACHE_LOCK:
        _MAX_WORKERS = max(1, int(max_workers))
        _MAX_QUEUE_SIZE = max(1, int(max_queue_size))
        _TASK_QUEUE = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
        _QUEUED_OR_IN_FLIGHT.clear()


def get_ai_metrics() -> dict:
    """Retorna snapshot das métricas de cache, fila, workers e latência."""
    with _CACHE_LOCK:
        completed = int(_METRICS["completed"])
        avg_ms = round(float(_METRICS["total_http_latency_ms"]) / completed, 2) if completed > 0 else 0.0
        alive_workers = sum(1 for t in _WORKER_THREADS if t.is_alive())
        return {
            "cache_hits": int(_METRICS["cache_hits"]),
            "cache_misses": int(_METRICS["cache_misses"]),
            "enqueued": int(_METRICS["enqueued"]),
            "deduplicated": int(_METRICS["deduplicated"]),
            "queue_dropped": int(_METRICS["queue_dropped"]),
            "worker_errors": int(_METRICS["worker_errors"]),
            "late_discarded": int(_METRICS["late_discarded"]),
            "completed": completed,
            "avg_http_latency_ms": avg_ms,
            "queue_size": _TASK_QUEUE.qsize(),
            "in_flight": len(_QUEUED_OR_IN_FLIGHT),
            "cache_size": len(_ENTRY_VERDICT_CACHE),
            "active_workers": alive_workers,
            "max_workers": _MAX_WORKERS,
            "max_queue_size": _MAX_QUEUE_SIZE,
        }


def clear_verdict_cache() -> None:
    """Limpa o cache de veredictos, fila e métricas (simula reinício limpo)."""
    global _WORKER_EPOCH, _TASK_QUEUE
    with _CACHE_LOCK:
        _WORKER_EPOCH += 1
        _ENTRY_VERDICT_CACHE.clear()
        _IN_FLIGHT_EVENTS.clear()
        _QUEUED_OR_IN_FLIGHT.clear()
        _TASK_QUEUE = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
        for k, value in _METRICS.items():
            _METRICS[k] = 0.0 if isinstance(value, float) else 0


def shutdown_ai_worker(wait: bool = True, timeout: float = 2.0) -> None:
    """Encerra ordenadamente os workers em background."""
    global _WORKER_EPOCH
    with _CACHE_LOCK:
        _WORKER_EPOCH += 1
        _STOP_EVENT.set()
        threads = list(_WORKER_THREADS)
        _WORKER_THREADS.clear()
        _QUEUED_OR_IN_FLIGHT.clear()
    if wait:
        deadline = time.time() + max(0.1, float(timeout))
        for t in threads:
            rem = max(0.01, deadline - time.time())
            t.join(timeout=rem)


atexit.register(shutdown_ai_worker, True, 1.0)


def _prune_cache_locked(now: float, timeframe_secs: int) -> None:
    """Remove entradas expiradas por tempo de vida e aplica limite máximo LRU simples."""
    expired_keys = [
        k for k, (ts, _) in _ENTRY_VERDICT_CACHE.items()
        if (now - ts) >= timeframe_secs
    ]
    for k in expired_keys:
        _ENTRY_VERDICT_CACHE.pop(k, None)
    while len(_ENTRY_VERDICT_CACHE) >= _MAX_CACHE_SIZE:
        oldest_key = min(_ENTRY_VERDICT_CACHE, key=lambda k: _ENTRY_VERDICT_CACHE[k][0])
        _ENTRY_VERDICT_CACHE.pop(oldest_key, None)


def _execute_ai_http_request(
    s: dict,
    pair: str,
    rate: float,
    ker: float,
    atr_pct: float,
    vol_z: float,
    regime_bull: bool,
    daily_drawdown_pct: float,
    mode: str,
    side: str,
    signal_tag: str,
    candle_bucket: int,
) -> dict:
    """Executa a chamada HTTP única à Mirai API com timeout separado e parsing estrito."""
    api_key = s.get("api_key", "").strip()
    endpoint = s.get("endpoint", "").strip()
    model = s.get("model", "claude-fable-5.1")
    min_conv = float(s.get("min_conviccao", 0.65))

    prompt_content = (
        "Você é o Chief Risk Officer AI (Guardião de Risco Cognitivo) de um fundo quantitativo.\n"
        "O motor matemático do Freqtrade pré-aprovou uma entrada com as seguintes métricas de mercado:\n\n"
        f"• Par: {pair}\n"
        f"• Lado: {side} | Sinal: {signal_tag or 'N/A'}\n"
        f"• Cotação Atual: {rate}\n"
        f"• KER (Kaufman Efficiency Ratio): {ker:.4f} (Ruído < 0.35 | Tendência Limpa >= 0.35)\n"
        f"• ATR Percentual: {atr_pct*100:.2f}%\n"
        f"• Volume Z-Score (MAD Baleia): {vol_z:.2f}\n"
        f"• Regime de Mercado: {'BULL EXPANSÃO' if regime_bull else 'LATERAL/CONSOLIDAÇÃO'}\n"
        f"• Drawdown Diário Atual: {daily_drawdown_pct*100:.2f}%\n"
        f"• Modo Operacional: {mode}\n\n"
        "Avalie se este trade tem alta convicção ou se aparenta falso rompimento/exaustão.\n"
        "RESPONDA ESTRITAMENTE EM JSON VÁLIDO no seguinte formato (sem texto antes ou depois):\n"
        '{"aprovado": true, "conviccao": 0.80, "motivo": "Justificativa analítica concisa em até 20 palavras"}'
    )

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt_content}],
        "temperature": 0.1,
        "max_tokens": 150,
    }

    err_msg = "Resposta não contém JSON válido"
    t0 = time.perf_counter()

    try:
        resp = requests.post(
            endpoint,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Freqtrade-AIGuardian/1.0",
            },
            timeout=(3.0, 8.0),
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        with _CACHE_LOCK:
            _METRICS["completed"] = int(_METRICS["completed"]) + 1
            _METRICS["total_http_latency_ms"] = float(_METRICS["total_http_latency_ms"]) + latency_ms

        if resp.status_code == 200:
            data = resp.json()
            choices = data.get("choices") if isinstance(data, dict) else None
            if not isinstance(choices, list) or not choices:
                err_msg = "Resposta 200 sem lista 'choices' válida"
            else:
                msg_obj = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
                content = str(msg_obj.get("content", "")).strip()

                json_match = re.search(r"\{.*\}", content, re.DOTALL)
                if json_match:
                    parsed = json.loads(json_match.group(0))
                    if not isinstance(parsed, dict) or "aprovado" not in parsed or "conviccao" not in parsed:
                        err_msg = "JSON sem campos obrigatórios ('aprovado', 'conviccao')"
                    elif not isinstance(parsed["aprovado"], bool):
                        err_msg = "Campo 'aprovado' não é booleano"
                    else:
                        aprovado_ia = parsed["aprovado"]
                        conviccao = float(parsed["conviccao"])
                        if not (0.0 <= conviccao <= 1.0):
                            err_msg = f"Convicção fora do intervalo [0,1]: {conviccao}"
                        else:
                            motivo = str(parsed.get("motivo", "Aprovado por confluência"))
                            aprovado_final = aprovado_ia and (conviccao >= min_conv)
                            if not aprovado_final:
                                if not aprovado_ia:
                                    motivo = f"VETO IA: {motivo}"
                                else:
                                    motivo = f"VETO CONVICÇÃO: {conviccao*100:.0f}% < Mínima {min_conv*100:.0f}% ({motivo})"

                            verdict = {
                                "aprovado": aprovado_final,
                                "conviccao": round(conviccao, 2),
                                "motivo": motivo,
                                "origem": "ia",
                                "pair": pair,
                                "mode": mode,
                                "side": side,
                                "signal_tag": signal_tag,
                                "candle_bucket": candle_bucket,
                                "ts": time.strftime("%H:%M:%S UTC"),
                            }

                            s["total_queries"] = s.get("total_queries", 0) + 1
                            if aprovado_final:
                                s["total_approvals"] = s.get("total_approvals", 0) + 1
                            else:
                                s["total_vetoes"] = s.get("total_vetoes", 0) + 1
                            s["last_verdict"] = verdict
                            update_state(**s)
                            return verdict
                else:
                    err_msg = f"Resposta 200 não contém JSON válido: {content[:80]}"
        else:
            err_msg = f"HTTP {resp.status_code}"
    except requests.RequestException as e:
        logger.warning("[AI GUARDIAN FAIL-CLOSED] Timeout/Rede (%s) -> Veto preventivo", e)
        err_msg = f"Timeout/Rede na API ({e})"
        with _CACHE_LOCK:
            _METRICS["worker_errors"] = int(_METRICS["worker_errors"]) + 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("[AI GUARDIAN FAIL-CLOSED] Exceção na consulta (%s) -> Veto preventivo", exc)
        err_msg = f"Erro na API ({exc})"
        with _CACHE_LOCK:
            _METRICS["worker_errors"] = int(_METRICS["worker_errors"]) + 1

    verdict = {
        "aprovado": False,
        "conviccao": 0.0,
        "motivo": f"Fail-closed: {err_msg}",
        "origem": "fail_closed",
        "pair": pair,
        "mode": mode,
        "side": side,
        "signal_tag": signal_tag,
        "candle_bucket": candle_bucket,
        "ts": time.strftime("%H:%M:%S UTC"),
    }
    update_state(last_verdict=verdict)
    return verdict


def _worker_loop(worker_epoch: int, task_queue: queue.Queue) -> None:
    """Loop contínuo de worker em background para pré-avaliação assíncrona da IA."""
    while not _STOP_EVENT.is_set():
        with _CACHE_LOCK:
            if worker_epoch != _WORKER_EPOCH:
                return
        try:
            task = task_queue.get(timeout=0.2)
        except queue.Empty:
            continue

        cache_key = task["cache_key"]
        try:
            now_before = time.time()
            # Rejeita antes da rede se o sinal já expirou ou o serviço foi reiniciado
            if task["epoch"] != _WORKER_EPOCH or (now_before - task["submitted_at"]) >= task["signal_ttl_secs"]:
                with _CACHE_LOCK:
                    _METRICS["late_discarded"] = int(_METRICS["late_discarded"]) + 1
                continue

            verdict = _execute_ai_http_request(
                s=task["state"],
                pair=task["pair"],
                rate=task["rate"],
                ker=task["ker"],
                atr_pct=task["atr_pct"],
                vol_z=task["vol_z"],
                regime_bull=task["regime_bull"],
                daily_drawdown_pct=task["daily_drawdown_pct"],
                mode=task["mode"],
                side=task["side"],
                signal_tag=task["signal_tag"],
                candle_bucket=task["candle_bucket"],
            )

            now_after = time.time()
            with _CACHE_LOCK:
                # Rejeição de resultados atrasados (pós-reinício, pós-expiração do sinal ou pós-virada da vela)
                if (
                    task["epoch"] != _WORKER_EPOCH
                    or _STOP_EVENT.is_set()
                    or (now_after - task["submitted_at"]) >= task["signal_ttl_secs"]
                    or (task["track_clock_bucket"] and int(now_after // task["tf_secs"]) != task["candle_bucket"])
                ):
                    _METRICS["late_discarded"] = int(_METRICS["late_discarded"]) + 1
                    continue

                if verdict.get("origem") == "ia":
                    _prune_cache_locked(now_after, task["tf_secs"])
                    _ENTRY_VERDICT_CACHE[cache_key] = (now_after, verdict)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[AI GUARDIAN WORKER] Exceção isolada no worker: %s", exc)
            with _CACHE_LOCK:
                _METRICS["worker_errors"] = int(_METRICS["worker_errors"]) + 1
        finally:
            with _CACHE_LOCK:
                _QUEUED_OR_IN_FLIGHT.discard(cache_key)
            task_queue.task_done()


def _ensure_workers_started_locked() -> None:
    """Inicia até _MAX_WORKERS threads daemon caso não estejam ativas."""
    _STOP_EVENT.clear()
    _WORKER_THREADS[:] = [t for t in _WORKER_THREADS if t.is_alive()]
    while len(_WORKER_THREADS) < _MAX_WORKERS:
        t = threading.Thread(
            target=_worker_loop,
            args=(_WORKER_EPOCH, _TASK_QUEUE),
            name=f"ai-guardian-worker-{len(_WORKER_THREADS)+1}",
            daemon=True,
        )
        t.start()
        _WORKER_THREADS.append(t)


def enqueue_ai_pre_evaluation(
    pair: str,
    rate: float,
    ker: float,
    atr_pct: float,
    vol_z: float,
    regime_bull: bool,
    daily_drawdown_pct: float,
    mode: str = "RenderQuantV1",
    side: str = "long",
    signal_tag: str = "",
    timeframe_secs: int = _DEFAULT_TIMEFRAME_SECS,
    candle_ts: float | None = None,
    signal_ttl_secs: float | None = None,
) -> bool:
    """Enfileira avaliação assíncrona da IA sem bloquear o chamador."""
    s = get_state()
    if not s.get("enabled", False):
        return False
    api_key = s.get("api_key", "").strip()
    endpoint = s.get("endpoint", "").strip()
    if not api_key or not endpoint:
        return False

    tf_secs = max(int(timeframe_secs or _DEFAULT_TIMEFRAME_SECS), 60)
    ttl = float(signal_ttl_secs) if signal_ttl_secs is not None else float(tf_secs)
    now = time.time()
    track_clock_bucket = candle_ts is None
    ref_ts = float(candle_ts) if candle_ts is not None else now
    candle_bucket = int(ref_ts // tf_secs)
    cache_key = (pair, mode, side, candle_bucket, str(signal_tag or ""))

    with _CACHE_LOCK:
        _prune_cache_locked(now, tf_secs)
        if cache_key in _ENTRY_VERDICT_CACHE or cache_key in _QUEUED_OR_IN_FLIGHT:
            _METRICS["deduplicated"] = int(_METRICS["deduplicated"]) + 1
            return False

        _ensure_workers_started_locked()
        task = {
            "cache_key": cache_key,
            "state": dict(s),
            "pair": pair,
            "rate": rate,
            "ker": ker,
            "atr_pct": atr_pct,
            "vol_z": vol_z,
            "regime_bull": regime_bull,
            "daily_drawdown_pct": daily_drawdown_pct,
            "mode": mode,
            "side": side,
            "signal_tag": str(signal_tag or ""),
            "candle_bucket": candle_bucket,
            "tf_secs": tf_secs,
            "signal_ttl_secs": ttl,
            "submitted_at": now,
            "track_clock_bucket": track_clock_bucket,
            "epoch": _WORKER_EPOCH,
        }
        try:
            _TASK_QUEUE.put_nowait(task)
            _QUEUED_OR_IN_FLIGHT.add(cache_key)
            _METRICS["enqueued"] = int(_METRICS["enqueued"]) + 1
            return True
        except queue.Full:
            _METRICS["queue_dropped"] = int(_METRICS["queue_dropped"]) + 1
            logger.warning("[AI GUARDIAN] Fila cheia (%d) – pré-avaliação para %s descartada.", _MAX_QUEUE_SIZE, pair)
            return False


def get_or_enqueue_ai_verdict(
    pair: str,
    rate: float,
    ker: float,
    atr_pct: float,
    vol_z: float,
    regime_bull: bool,
    daily_drawdown_pct: float,
    mode: str = "RenderQuantV1",
    side: str = "long",
    signal_tag: str = "",
    timeframe_secs: int = _DEFAULT_TIMEFRAME_SECS,
    candle_ts: float | None = None,
    signal_ttl_secs: float | None = None,
) -> dict:
    """
    Leitura O(1) não bloqueante para confirm_trade_entry():
    - Nunca faz chamada HTTP nem aguarda worker.
    - Retorna o veredicto em cache se válido para o par/estratégia/lado/vela/sinal exato.
    - Caso contrário, enfileira em background e retorna fail-closed ('pending_async').
    """
    s = get_state()
    if not s.get("enabled", False):
        return {
            "aprovado": True,
            "conviccao": 1.0,
            "motivo": "IA Guardian pausada no Telegram",
            "origem": "bypass",
        }
    if not s.get("api_key", "").strip() or not s.get("endpoint", "").strip():
        return {
            "aprovado": True,
            "conviccao": 1.0,
            "motivo": "Chave da IA não configurada (modo passivo)",
            "origem": "bypass",
        }

    tf_secs = max(int(timeframe_secs or _DEFAULT_TIMEFRAME_SECS), 60)
    ttl = float(signal_ttl_secs) if signal_ttl_secs is not None else float(tf_secs)
    now = time.time()
    ref_ts = float(candle_ts) if candle_ts is not None else now
    candle_bucket = int(ref_ts // tf_secs)
    cache_key = (pair, mode, side, candle_bucket, str(signal_tag or ""))

    with _CACHE_LOCK:
        _prune_cache_locked(now, tf_secs)
        cached = _ENTRY_VERDICT_CACHE.get(cache_key)
        if cached is not None:
            cached_ts, cached_verdict = cached
            if (now - cached_ts) < ttl:
                _METRICS["cache_hits"] = int(_METRICS["cache_hits"]) + 1
                return dict(cached_verdict)
        _METRICS["cache_misses"] = int(_METRICS["cache_misses"]) + 1

    enqueue_ai_pre_evaluation(
        pair=pair,
        rate=rate,
        ker=ker,
        atr_pct=atr_pct,
        vol_z=vol_z,
        regime_bull=regime_bull,
        daily_drawdown_pct=daily_drawdown_pct,
        mode=mode,
        side=side,
        signal_tag=signal_tag,
        timeframe_secs=tf_secs,
        candle_ts=candle_ts,
        signal_ttl_secs=ttl,
    )
    return {
        "aprovado": False,
        "conviccao": 0.0,
        "motivo": "Fail-closed: avaliação IA assíncrona em andamento/pendente",
        "origem": "pending_async",
        "pair": pair,
        "mode": mode,
        "candle_bucket": candle_bucket,
        "ts": time.strftime("%H:%M:%S UTC"),
    }


def consult_ai_guardian(
    pair: str,
    rate: float,
    ker: float,
    atr_pct: float,
    vol_z: float,
    regime_bull: bool,
    daily_drawdown_pct: float,
    mode: str = "RenderQuantV1",
    side: str = "long",
    signal_tag: str = "",
    timeframe_secs: int = _DEFAULT_TIMEFRAME_SECS,
    candle_ts: float | None = None,
) -> dict:
    """
    Consulta direta da IA com deduplicação in-flight e cache (para testes/diagnóstico).
    No caminho crítico de entrada do Freqtrade, utilize get_or_enqueue_ai_verdict().
    """
    s = get_state()

    if not s.get("enabled", False):
        return {
            "aprovado": True,
            "conviccao": 1.0,
            "motivo": "IA Guardian pausada no Telegram",
            "origem": "bypass",
        }

    api_key = s.get("api_key", "").strip()
    endpoint = s.get("endpoint", "").strip()
    if not api_key or not endpoint:
        return {
            "aprovado": True,
            "conviccao": 1.0,
            "motivo": "Chave da IA não configurada (modo passivo)",
            "origem": "bypass",
        }

    tf_secs = max(int(timeframe_secs or _DEFAULT_TIMEFRAME_SECS), 60)
    now = time.time()
    ref_ts = float(candle_ts) if candle_ts is not None else now
    candle_bucket = int(ref_ts // tf_secs)
    cache_key = (pair, mode, side, candle_bucket, str(signal_tag or ""))

    is_leader = False
    in_flight_ev: threading.Event | None = None

    while True:
        with _CACHE_LOCK:
            _prune_cache_locked(now, tf_secs)
            cached = _ENTRY_VERDICT_CACHE.get(cache_key)
            if cached is not None:
                cached_ts, cached_verdict = cached
                if (now - cached_ts) < tf_secs:
                    _METRICS["cache_hits"] = int(_METRICS["cache_hits"]) + 1
                    return dict(cached_verdict)
            existing_ev = _IN_FLIGHT_EVENTS.get(cache_key)
            if existing_ev is None:
                in_flight_ev = threading.Event()
                _IN_FLIGHT_EVENTS[cache_key] = in_flight_ev
                is_leader = True
                break
        existing_ev.wait(timeout=5.0)
        now = time.time()
        with _CACHE_LOCK:
            cached = _ENTRY_VERDICT_CACHE.get(cache_key)
            if cached is not None:
                _METRICS["cache_hits"] = int(_METRICS["cache_hits"]) + 1
                return dict(cached[1])
            if cache_key not in _IN_FLIGHT_EVENTS:
                in_flight_ev = threading.Event()
                _IN_FLIGHT_EVENTS[cache_key] = in_flight_ev
                is_leader = True
                break

    try:
        with _CACHE_LOCK:
            _METRICS["cache_misses"] = int(_METRICS["cache_misses"]) + 1
        verdict = _execute_ai_http_request(
            s=s,
            pair=pair,
            rate=rate,
            ker=ker,
            atr_pct=atr_pct,
            vol_z=vol_z,
            regime_bull=regime_bull,
            daily_drawdown_pct=daily_drawdown_pct,
            mode=mode,
            side=side,
            signal_tag=str(signal_tag or ""),
            candle_bucket=candle_bucket,
        )
        if verdict.get("origem") == "ia":
            with _CACHE_LOCK:
                _prune_cache_locked(time.time(), tf_secs)
                _ENTRY_VERDICT_CACHE[cache_key] = (time.time(), verdict)
        return dict(verdict)
    finally:
        if is_leader and in_flight_ev is not None:
            with _CACHE_LOCK:
                _IN_FLIGHT_EVENTS.pop(cache_key, None)
                in_flight_ev.set()


def consult_ai_exit(
    pair: str,
    current_profit_pct: float,
    open_duration_min: float,
    ker: float,
    atr_pct: float,
    regime_bull: bool,
) -> dict:
    """
    Consulta o Claude Fable 5.1 via Mirai API para avaliar encerramento antecipado de posição aberta.
    Retorno garantido sem exceção:
    {"sair": bool, "motivo": str, "origem": "ia" | "bypass" | "fallback"}
    """
    s = get_state()
    if not s.get("enabled", False) or not s.get("enable_ai_exits", True):
        return {"sair": False, "motivo": "IA de saída pausada", "origem": "bypass"}

    api_key = s.get("api_key", "").strip()
    endpoint = s.get("endpoint", "").strip()
    model = s.get("model", "claude-fable-5.1")

    if not api_key or not endpoint:
        return {"sair": False, "motivo": "Sem chave configurada", "origem": "bypass"}

    prompt_content = (
        "Você é o Chief Risk Officer AI avaliando se deve ENCERRAR ANTECIPADAMENTE uma posição aberta no Freqtrade para embolsar lucro ou evitar reversão violenta.\n\n"
        f"• Par: {pair}\n"
        f"• Lucro Líquido Atual: {current_profit_pct*100:+.2f}%\n"
        f"• Duração do Trade: {open_duration_min:.0f} minutos\n"
        f"• KER Atual (Eficiência da Tendência): {ker:.4f} (Ruído < 0.25 | Tendência Limpa >= 0.35)\n"
        f"• ATR Volatilidade: {atr_pct*100:.2f}%\n"
        f"• Regime de Mercado: {'BULL EXPANSÃO' if regime_bull else 'LATERAL/CONSOLIDAÇÃO'}\n\n"
        "Avalie se a posição deve ser encerrada AGORA (a mercado) para proteger lucro contra reversão ou cortar estagnação antes de virar prejuízo.\n"
        "RESPONDA ESTRITAMENTE EM JSON VÁLIDO no seguinte formato (sem texto antes ou depois):\n"
        '{"sair": true, "motivo": "Justificativa analítica concisa em até 15 palavras"}'
    )

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt_content}],
        "temperature": 0.1,
        "max_tokens": 120,
    }

    try:
        resp = requests.post(
            endpoint,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Freqtrade-AIGuardian/1.0",
            },
            timeout=(2.0, 2.5),
        )
        if resp.status_code == 200:
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()

            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group(0))
                sair = bool(parsed.get("sair", False))
                motivo = str(parsed.get("motivo", "Encerramento recomendado pela IA"))

                if sair:
                    s["total_ai_exits"] = s.get("total_ai_exits", 0) + 1
                    update_state(**s)

                return {
                    "sair": sair,
                    "motivo": motivo,
                    "origem": "ia",
                }

    except Exception as exc:  # noqa: BLE001
        logger.debug("[AI GUARDIAN EXIT] Erro/Timeout na consulta de saída: %s", exc)

    return {"sair": False, "motivo": "Fallback passivo (mantém ordens no livro)", "origem": "fallback"}


def test_ai_connection() -> tuple[bool, str]:
    """Testa a conectividade da API com latência real medida em milissegundos."""
    s = get_state()
    api_key = s.get("api_key", "").strip()
    endpoint = s.get("endpoint", "").strip()
    if not api_key:
        return False, "❌ Chave de API não informada."
    if not endpoint:
        return False, "❌ Endpoint não configurado."

    t0 = time.time()
    try:
        payload = {
            "model": s.get("model", "claude-fable-5.1"),
            "messages": [{"role": "user", "content": 'Responda estritamente em JSON: {"status": "ok"}'}],
            "max_tokens": 30,
        }
        resp = requests.post(
            endpoint,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Freqtrade-AIGuardian/1.0",
            },
            timeout=12.0,
        )
        latency_ms = int((time.time() - t0) * 1000)
        if resp.status_code == 200:
            return True, f"✅ Conexão OK! Modelo: `{s.get('model')}` | Latência: `{latency_ms}ms`"
        return False, f"❌ Erro de status HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"❌ Erro de conexão ({exc})"


def format_ai_guardian_text() -> str:
    """Gera o painel visual formatado em Markdown para o Telegram."""
    s = get_state()
    enabled = s.get("enabled", False)
    status_emoji = "🟢 *ATIVO (Veto Cognitivo Ligado)*" if enabled else "🔴 *PAUSADO (Bypass Geral)*"
    min_conv = int(s.get("min_conviccao", 0.65) * 100)
    key_masked = mask_key(s.get("api_key", ""))
    model = s.get("model", "claude-fable-5.1")

    queries = s.get("total_queries", 0)
    approvals = s.get("total_approvals", 0)
    vetoes = s.get("total_vetoes", 0)
    ai_exits = s.get("total_ai_exits", 0)

    last_v = s.get("last_verdict")
    if last_v and isinstance(last_v, dict):
        status_v = "✅ Aprovado" if last_v.get("aprovado") else "⛔ VETADO"
        pair_v = last_v.get("pair", "N/A")
        conv_v = int(last_v.get("conviccao", 0) * 100)
        motivo_v = last_v.get("motivo", "N/A")
        ts_v = last_v.get("ts", "")
        safe_motivo = str(motivo_v).replace("_", " ").replace("*", "").replace("`", "'")
        last_str = f"{status_v} em `{pair_v}` ({conv_v}%) às `{ts_v}`\n↳ _{safe_motivo}_"
    else:
        last_str = "_Aguardando primeiro sinal de entrada do robô..._"

    ts_now = time.strftime("%H:%M:%S")

    return (
        f"🧠 *Guardião de Risco Cognitivo ({model})*\n\n"
        f"• *Status:* {status_emoji}\n"
        f"• *Motor Cognitivo:* `{model}`\n"
        f"• *API Key:* `{key_masked}`\n"
        f"• *Convicção Mínima:* `{min_conv}%`\n"
        f"• *Saída Inteligente (Profit Shield):* `🟢 Ativa (15m)`\n"
        f"• *Auditoria:* `{queries}` analisadas (`{approvals}` aprovadas | `{vetoes}` vetadas | `{ai_exits}` saídas)\n\n"
        f"📋 *Último Parecer:* \n{last_str}\n\n"
        f"🕒 *Atualizado:* `{ts_now}`\n\n"
        f"👇 *Controle a IA em tempo real pelos botões abaixo:*"
    )


def build_ai_guardian_inline_markup():
    """Constrói os botões interativos do Telegram."""
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError:
        class InlineKeyboardButton:
            def __init__(self, text, callback_data=None):
                self.text = text
                self.callback_data = callback_data
        class InlineKeyboardMarkup:
            def __init__(self, inline_keyboard):
                self.inline_keyboard = inline_keyboard

    s = get_state()
    enabled = s.get("enabled", False)
    curr_conv = float(s.get("min_conviccao", 0.65))

    # Botão de Ativar/Pausar com status explícito
    if enabled:
        toggle_btn = InlineKeyboardButton("🟢 IA ATIVADA (Toque para Pausar)", callback_data="unk_ai:toggle")
    else:
        toggle_btn = InlineKeyboardButton("🔴 IA PAUSADA (Toque para ATIVAR)", callback_data="unk_ai:toggle")

    # Botões de patamares de convicção
    conv_options = [0.60, 0.65, 0.70, 0.80]
    conv_btns = []
    for c in conv_options:
        mark = "🎯" if abs(c - curr_conv) < 0.01 else ""
        label = f"{mark} {int(c*100)}%"
        conv_btns.append(InlineKeyboardButton(label.strip(), callback_data=f"unk_ai:conv:{c:.2f}"))

    return InlineKeyboardMarkup([
        [toggle_btn],
        conv_btns,
        [
            InlineKeyboardButton("🔑 Inserir API Key", callback_data="unk_ai:key"),
            InlineKeyboardButton("🧪 Testar Conexão", callback_data="unk_ai:test"),
        ],
        [
            InlineKeyboardButton("📋 Ver Parecer", callback_data="unk_ai:verdict"),
            InlineKeyboardButton("🔄 Atualizar", callback_data="unk_ai:refresh"),
        ],
        [
            InlineKeyboardButton("🏦 Ir para Menu da Corretora", callback_data="unk_ex:refresh"),
            InlineKeyboardButton("🏠 Menu Principal", callback_data="unk_nav:menu"),
        ],
    ])
