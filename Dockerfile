FROM freqtradeorg/freqtrade:stable

USER root
RUN mkdir -p /freqtrade/user_data/strategies /freqtrade/user_data/logs && chown -R ftuser:ftuser /freqtrade/user_data

USER ftuser
WORKDIR /freqtrade

RUN pip install --no-cache-dir psycopg2-binary

COPY --chown=ftuser:ftuser start_bot.py /freqtrade/start_bot.py
COPY --chown=ftuser:ftuser user_data/config.render.json /freqtrade/user_data/config.render.json
COPY --chown=ftuser:ftuser user_data/strategies/RenderQuantV1.py /freqtrade/user_data/strategies/RenderQuantV1.py

EXPOSE 10000

ENTRYPOINT []
CMD ["python3", "-u", "/freqtrade/start_bot.py"]
