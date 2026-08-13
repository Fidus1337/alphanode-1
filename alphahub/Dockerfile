# AlphaHub — the licence + vault server, containerized.
# Build context is the REPO ROOT (the hub reuses alphanode/vault.py for the sealed-box crypto):
#   docker build -f alphahub/Dockerfile .
# The deploy kit (deploy/docker-compose.yml) does this for you.
FROM python:3.12-slim

WORKDIR /app
COPY alphahub/requirements.txt alphahub/requirements.txt
RUN pip install --no-cache-dir -r alphahub/requirements.txt

COPY alphahub alphahub
COPY alphanode/vault.py alphanode/vault.py

# state lives on a volume: the SQLite db and THE vault private key (back it up!)
ENV ALPHAHUB_DB=/data/hub.db \
    ALPHAHUB_VAULT_KEY=/data/vault_key
VOLUME /data

EXPOSE 8790
# ALPHAHUB_WEBHOOK_SECRET must come from the environment — the server refuses to start without
# it (fail closed: the webhook grants paid plans).
CMD ["uvicorn", "alphahub.server:app", "--host", "0.0.0.0", "--port", "8790"]
