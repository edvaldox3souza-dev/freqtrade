"""Wrapper enxuto e seguro para inicializar o Freqtrade no Render (Secret File opcional).

Lê todas as credenciais exclusivamente de variáveis de ambiente (os.environ) e força
a execução da estratégia RenderQuantV1 com config.render.json e banco Neon PostgreSQL.
"""

import os
import sys


def build_freqtrade_argv(db_url: str) -> list[str]:
    return [
        "freqtrade",
        "trade",
        "--config",
        "/freqtrade/user_data/config.render.json",
        "--userdir",
        "/freqtrade/user_data",
        "--strategy",
        "RenderQuantV1",
        "--db-url",
        db_url,
    ]


def main() -> None:
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        print("ERROR: DATABASE_URL não definida nas variáveis de ambiente.", file=sys.stderr, flush=True)
        raise SystemExit(1)

    os.environ["FREQTRADE__API_SERVER__LISTEN_PORT"] = os.environ.get("PORT", "10000")
    sys.argv = build_freqtrade_argv(db_url)

    from freqtrade.main import main as ft_main

    ft_main()


if __name__ == "__main__":
    main()