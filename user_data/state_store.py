"""
State Store — Persistência de estado em Postgres (Neon) com cache local em arquivo.

Resolve a efemeridade do filesystem do Render: qualquer redeploy apaga arquivos,
mas o banco Neon sobrevive. Estratégia: DB é fonte da verdade, arquivo é cache.
"""
import json
import logging
import os
import pathlib
import threading
import time

logger = logging.getLogger("state_store")

_STATE_LOCK = threading.RLock()
_LOCAL_STATE = threading.local()
_TABLE_ENSURED = False
_MEM_CACHE: dict[str, tuple[float, dict]] = {}
_DEFAULT_CACHE_TTL_SECS = 5.0


def clear_state_cache() -> None:
    """Limpa o cache em memória do state_store (útil em testes)."""
    with _STATE_LOCK:
        _MEM_CACHE.clear()


def _db_dsn() -> str | None:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url or "postgres" not in url:
        return None
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url.replace("postgresql+psycopg2://", "postgresql://")


def _connect():
    import psycopg2

    dsn = _db_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL não configurada")
    return psycopg2.connect(dsn, connect_timeout=10)


def _ensure_table(conn) -> None:
    global _TABLE_ENSURED
    if _TABLE_ENSURED:
        return
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS bot_state ("
            " key TEXT PRIMARY KEY,"
            " value JSONB NOT NULL,"
            " updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")"
        )
    conn.commit()
    _TABLE_ENSURED = True


def db_get(key: str) -> dict | None:
    _LOCAL_STATE.db_get_failed = False
    conn = None
    try:
        conn = _connect()
        with conn:
            _ensure_table(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM bot_state WHERE key = %s", (key,))
                row = cur.fetchone()
                if not row:
                    return None
                val = row[0]
                if isinstance(val, dict):
                    return val
                if isinstance(val, str):
                    return json.loads(val)
                return val
    except Exception as exc:
        _LOCAL_STATE.db_get_failed = True
        logger.debug("state_store db_get(%s) indisponível: %s", key, exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def db_set(key: str, value: dict) -> None:
    conn = None
    try:
        conn = _connect()
        with conn:
            _ensure_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO bot_state (key, value, updated_at) VALUES (%s, %s, now()) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                    (key, json.dumps(value, ensure_ascii=False)),
                )
    except Exception as exc:
        logger.warning("state_store db_set(%s) falhou (mantendo cache local): %s", key, exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _atomic_write(path: pathlib.Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        logger.debug("state_store cache write %s falhou: %s", path, exc)


def load_state(
    key: str,
    file_paths: list[pathlib.Path],
    default: dict,
    cache_ttl_secs: float = _DEFAULT_CACHE_TTL_SECS,
) -> dict:
    """Carrega do cache em memória/DB; se ausente no DB, tenta arquivo e migra para o DB."""
    now = time.monotonic() if "time" in globals() else 0.0
    with _STATE_LOCK:
        if cache_ttl_secs > 0 and key in _MEM_CACHE:
            ts, cached_val = _MEM_CACHE[key]
            if (now - ts) < cache_ttl_secs:
                return {**default, **cached_val}
        data = db_get(key)
        if data is not None:
            _MEM_CACHE[key] = (now, dict(data))
            return {**default, **data}
        for p in file_paths:
            try:
                if p.exists():
                    data = json.loads(p.read_text(encoding="utf-8"))
                    _MEM_CACHE[key] = (now, dict(data))
                    if not getattr(_LOCAL_STATE, "db_get_failed", False):
                        db_set(key, data)  # migração transparente para o Neon apenas quando DB respondeu sem erro
                    return {**default, **data}
            except Exception as exc:
                logger.debug("state_store cache read %s falhou: %s", p, exc)
                continue
        return dict(default)


def save_state(key: str, value: dict, file_paths: list[pathlib.Path]) -> dict:
    """Grava no DB (fonte da verdade), cache em memória e nos arquivos locais."""
    now = time.monotonic() if "time" in globals() else 0.0
    with _STATE_LOCK:
        _MEM_CACHE[key] = (now, dict(value))
        db_set(key, value)
        for p in file_paths:
            _atomic_write(p, value)
        return value


def save_state_async(key: str, value: dict, file_paths: list[pathlib.Path]) -> None:
    """Atualiza cache e arquivo local imediatamente e persiste no Postgres em background."""
    now = time.monotonic() if "time" in globals() else 0.0
    snapshot = dict(value)
    with _STATE_LOCK:
        _MEM_CACHE[key] = (now, snapshot)
        for p in file_paths:
            _atomic_write(p, snapshot)
    threading.Thread(target=db_set, args=(key, snapshot), daemon=True, name=f"state-save-{key}").start()
