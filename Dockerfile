# Headless AlphaNode — everything the GUI does, minus the desktop UI: the evolutionary search node
# with a live web status page, the Binance data fetcher, portfolio builder, the signal API, and
# the forward track (a running node steps enrolled strategies every 5 min; ALPHANODE_FORWARD=0
# opts out). Drive it through the CLI:  docker run ... alphanode <command>  (run / fetch /
# top [--stats] / status / forward / portfolio / signal / export). See docker-compose.yml
# and the README's Docker section.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NUMBA_CACHE_DIR=/tmp/numba \
    ALPHANODE_STATE_DIR=/data/state \
    ALPHANODE_DATA=/data/data.pickle

WORKDIR /app

# deps first (layer cache) — the runtime graph is intentionally narrow (no GUI)
COPY docker/requirements-docker.txt /app/docker/requirements-docker.txt
RUN pip install -r /app/docker/requirements-docker.txt

# engine + app sources; data.pickle + evolution/config.ini ship as the default snapshot
COPY evolution/    /app/evolution/
COPY quantpylib/   /app/quantpylib/
COPY alphanode/    /app/alphanode/
COPY fetch_data.py /app/fetch_data.py
COPY data.pickle   /app/data.pickle
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
RUN chmod +x /app/docker/entrypoint.sh

EXPOSE 8787 8799
VOLUME ["/data"]

# entrypoint seeds /data from the bundled snapshot, then runs the CLI; CMD is the default subcommand
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["run"]
