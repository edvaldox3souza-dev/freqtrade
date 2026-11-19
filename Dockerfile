FROM freqtradeorg/freqtrade:stable

USER root
RUN mkdir -p /freqtrade/user_data/strategies /freqtrade/user_data/logs && chown -R ftuser:ftuser /freqtrade/user_data

USER ftuser
WORKDIR /freqtrade

RUN pip install --no-cache-dir psycopg2-binary

COPY --chown=ftuser:ftuser user_data/config.render.json /freqtrade/user_data/config.render.json
COPY --chown=ftuser:ftuser user_data/strategies/RenderQuantV1.py /freqtrade/user_data/strategies/RenderQuantV1.py

EXPOSE 10000

ENTRYPOINT []
CMD ["sh", "-c", "test -n \"${DATABASE_URL}\" || { echo 'ERROR: DATABASE_URL not set'; exit 1; }; export FREQTRADE__API_SERVER__LISTEN_PORT=\"${PORT:-10000}\"; exec freqtrade trade --config /freqtrade/user_data/config.render.json --userdir /freqtrade/user_data --strategy RenderQuantV1 --db-url \"${DATABASE_URL}\""]
