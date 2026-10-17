#!/bin/bash
# aplay command wrapper -- plays $1.wav, found anywhere under ./sounds/ (including subfolders)

file=$(find ./sounds -type f -name "$1.wav" -print -quit)
if [ -z "$file" ]; then
    echo "playwav.sh: no file named '$1.wav' found under ./sounds/" >&2
    exit 1
fi

aplay -D plughw:2,0 "$file"

