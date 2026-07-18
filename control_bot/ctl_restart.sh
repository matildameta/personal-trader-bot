#!/bin/bash
# Restart ONLY the control bot (traderctl screen), engine untouched.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
VENV="$ROOT/.venv/bin/python"
LOG="$ROOT/control_bot/ctl.log"

# kill existing screen if alive
screen -S traderctl -X quit 2>/dev/null
sleep 2

# start fresh detached screen
cd "$ROOT/control_bot"
screen -dmS traderctl bash -c "$VENV -m src.bot 2>&1 | tee $LOG"
echo "control bot restarted at $(date)"
