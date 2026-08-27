#!/bin/bash
# Check if venv folder exists, if not, create it
if [ ! -d ".venv" ]; then
    python -m venv --system-site-packages .venv
    echo "Created .venv"
fi

# Activate the venv
source .venv/bin/activate

# Optional: Upgrade pip or install requirements
#pip install --upgrade pip
