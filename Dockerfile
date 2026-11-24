FROM freqtradeorg/freqtrade:stable

USER root
RUN apt-get update && apt-get install -y --no-install-recommends tini && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /freqtrade/user_data/strategies /freqtrade/user_data/logs && chown -R ftuser:ftuser /freqtrade/user_data

USER ftuser
WORKDIR /freqtrade

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MALLOC_ARENA_MAX=2 \
    BOOT_VERSION=20261122_v4_clean_boot_no_secrets

RUN pip install --no-cache-dir psycopg2-binary

COPY --chown=ftuser:ftuser start_bot.py /freqtrade/start_bot.py
COPY --chown=ftuser:ftuser user_data/ /freqtrade/user_data/

EXPOSE 10000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-u", "/freqtrade/start_bot.py"]
