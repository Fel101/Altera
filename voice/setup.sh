#!/bin/sh
# Install Altera's local voice service: its own environment in voice/.venv, then Kokoro's model and voices.
# Kokoro needs Python 3.10, 3.11 or 3.12. Set PYTHON to choose one, for example PYTHON=python3.12 sh voice/setup.sh
set -e
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python=${PYTHON:-python3}
if [ ! -x "$here/.venv/bin/python" ]; then
    echo "Making $here/.venv with $python"
    "$python" -m venv "$here/.venv"
fi
"$here/.venv/bin/python" -m pip install --upgrade pip
"$here/.venv/bin/python" -m pip install -r "$here/requirements.txt"
"$here/.venv/bin/python" "$here/serve.py" --download
echo
echo "The voice service is installed. In Altera's Settings, switch Voice on."
