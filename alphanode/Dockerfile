# AlphaNode — headless alpha-search node (CLI, no GUI). Build from the repository ROOT:
#   docker build -f alphanode/Dockerfile -t alphanode .
# Run (node):               docker run -v alphanode-data:/data -p 8787:8787 alphanode
# Other commands:           docker run -v alphanode-data:/data alphanode top --sort test
#                           docker run -v alphanode-data:/data alphanode fetch --top 150
#                           docker run -v alphanode-data:/data alphanode status
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# numpy/pandas — engine; requests/aiosonic/orjson/onecache — Binance data fetcher (fetch command).
# aiosonic is pinned: quantpylib is verified on 0.31.x (the API changed in 1.x).
RUN pip install --no-cache-dir numpy pandas requests "aiosonic==0.31.1" orjson onecache

# engine core + data + node + data fetcher
COPY evolution ./evolution
COPY quantpylib ./quantpylib
COPY data.pickle ./data.pickle
COPY fetch_data.py ./fetch_data.py
COPY alphanode ./alphanode

ENV ALPHANODE_STATE_DIR=/data
RUN mkdir -p /data && useradd -m app && chown -R app /app /data
USER app

VOLUME /data
EXPOSE 8787

# CLI-first: entrypoint is cli.py, default command is `run` (continuous search in the foreground).
# Override with:  docker run … alphanode top   |   … fetch --top 150   |   … status
ENTRYPOINT ["python", "alphanode/cli.py"]
CMD ["run"]
