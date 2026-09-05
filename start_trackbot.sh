#!/bin/bash
# Launched from a terminal at desktop login (see ~/.config/labwc/autostart).
# Sets up the venv and runs the main trackbot program; keeps the terminal open
# afterward so any errors/output stay visible instead of the window vanishing.
# Everything printed in this terminal is also written to a timestamped log file
# under ~/Documents/TrackBotLogs/.

cd "$(dirname "$0")" || exec bash

# Play a startup chime in the background so it doesn't delay the rest of setup.
# --volume 0.9 matches trackbot_audio.py's PLAYBACK_VOLUME.
pw-play --volume 0.9 ./sounds/startingtrackbot.wav &

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

# Only replace ourselves with a fresh shell (to keep the window open) when we
# were launched as a terminal's own command -- e.g. lxterminal's autostart
# (see ~/.config/labwc/autostart), where there's no underlying shell to return
# to and the window would otherwise vanish along with any errors on screen.
# Run manually from an already-open terminal, just return to that shell's
# prompt instead of dropping into a nested one.
parent_comm=$(cat "/proc/$PPID/comm" 2>/dev/null)
if [ "$parent_comm" = "lxterminal" ]; then
    exec bash
fi
