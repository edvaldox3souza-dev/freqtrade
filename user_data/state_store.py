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

logger = logging.getLogger("state_store")

_STATE_LOCK = threading.RLock()


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
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS bot_state ("
            " key TEXT PRIMARY KEY,"
            " value JSONB NOT NULL,"
            " updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")"
        )
    conn.commit()


def db_get(key: str) -> dict | None:
    conn = None
    try:
        conn = _connect()
        with conn:
            _ensure_table(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM bot_state WHERE key = %s", (key,))
                row = cur.fetchone()
                return json.loads(row[0]) if row else None
    except Exception as exc:
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


def load_state(key: str, file_paths: list[pathlib.Path], default: dict) -> dict:
    """Carrega do DB; se ausente no DB, tenta arquivo e migra para o DB."""
    with _STATE_LOCK:
        data = db_get(key)
        if data is not None:
            return {**default, **data}
        for p in file_paths:
            try:
                if p.exists():
                    data = json.loads(p.read_text(encoding="utf-8"))
                    db_set(key, data)  # migração transparente para o Neon
                    return {**default, **data}
            except Exception:
                continue
        return dict(default)


def save_state(key: str, value: dict, file_paths: list[pathlib.Path]) -> dict:
    """Grava no DB (fonte da verdade) e nos arquivos (cache)."""
    with _STATE_LOCK:
        db_set(key, value)
        for p in file_paths:
            _atomic_write(p, value)
        return value
