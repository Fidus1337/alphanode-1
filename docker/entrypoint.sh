#!/bin/sh
# Prepare the writable /data volume, then hand off to the AlphaNode CLI.
# Args are the CLI subcommand + flags, e.g.:  run  ·  fetch --top 150  ·  top -n 20  ·  status
set -e

: "${ALPHANODE_STATE_DIR:=/data/state}"
: "${ALPHANODE_DATA:=/data/data.pickle}"
: "${NUMBA_CACHE_DIR:=/tmp/numba}"

mkdir -p "$ALPHANODE_STATE_DIR" "$(dirname "$ALPHANODE_DATA")" "$NUMBA_CACHE_DIR"

# seed the persistent data volume from the snapshot baked into the image (first run only);
# a later `fetch` overwrites exactly this file, so fresh data is picked up automatically.
if [ ! -f "$ALPHANODE_DATA" ] && [ -f /app/data.pickle ]; then
    cp /app/data.pickle "$ALPHANODE_DATA"
fi

export ALPHANODE_STATE_DIR ALPHANODE_DATA NUMBA_CACHE_DIR
exec python /app/alphanode/cli.py "$@"
