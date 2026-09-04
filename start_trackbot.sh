#!/bin/bash
# Launched from a terminal at desktop login (see ~/.config/labwc/autostart).
# Sets up the venv and runs the main trackbot program; keeps the terminal open
# afterward so any errors/output stay visible instead of the window vanishing.
# Everything printed in this terminal is also written to a timestamped log file
# under ~/Documents/TrackBotLogs/.

cd "$(dirname "$0")" || exec bash

LOG_DIR="/home/trackbot/Documents/TrackBotLogs"
LOG_FILE="$LOG_DIR/trackbot_$(date +%Y-%m-%d_%H-%M-%S).log"
if mkdir -p "$LOG_DIR" 2>/dev/null && [ -w "$LOG_DIR" ]; then
    # Redirects this shell's stdout+stderr through tee for the rest of the script,
    # so everything below (including venv.sh's own output) is both shown and logged.
    exec > >(tee -a "$LOG_FILE") 2>&1
    echo "Logging to $LOG_FILE"
else
    echo "WARNING: could not write to $LOG_DIR -- continuing without file logging."
fi

source venv.sh
sleep 1

echo
echo "Starting run_trackbot.py"
python3 run_trackbot.py --show-preview --record-preview
echo "run_trackbot.py exited (status $?)"

exec bash
