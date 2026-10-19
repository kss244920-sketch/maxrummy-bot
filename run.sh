#!/usr/bin/env bash
# Runner for MaxRummy & YonoGames Bot

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Check virtual environment
if [ -d "/home/piyush/venv" ]; then
    PYTHON="/home/piyush/venv/bin/python3"
elif [ -d "$DIR/venv" ]; then
    PYTHON="$DIR/venv/bin/python3"
else
    PYTHON="python3"
fi

echo "Starting MaxRummy & YonoGames Bot with $PYTHON..."
exec "$PYTHON" maxrummy_bot.py
