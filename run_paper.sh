#!/usr/bin/env bash
# Ежедневный запуск paper trading (для cron).
# cron стартует с минимальным окружением, поэтому явные абсолютные пути + cd.
set -euo pipefail
PROJ="/home/yurii/Documents/hanguk_trades"
cd "$PROJ" || exit 1
echo "===== $(date -u '+%Y-%m-%d %H:%M:%S UTC') =====" >> paper_run.log
"$PROJ/.venv/bin/python" paper_trade.py >> paper_run.log 2>&1
echo "" >> paper_run.log
