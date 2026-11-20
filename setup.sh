#!/bin/sh
set -eu
script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
ALTERA_SETUP_PYTHON=${ALTERA_SETUP_PYTHON:-}
if [ -n "$ALTERA_SETUP_PYTHON" ]; then
    exec "$ALTERA_SETUP_PYTHON" -I -B "$script_dir/scripts/setup_altera.py" "$@"
fi
if [ -x "$script_dir/.venv/bin/python" ]; then
    exec "$script_dir/.venv/bin/python" -I -B "$script_dir/scripts/setup_altera.py" "$@"
fi
# Python 3.11, 3.12 or 3.13. 3.12 is tried first: the local voice runs on it too.
for version in 3.12 3.13 3.11; do
    if command -v "python$version" >/dev/null 2>&1; then
        exec "python$version" -I -B "$script_dir/scripts/setup_altera.py" "$@"
    fi
done
# Setup checks this Python's version itself and says so if it is not 3.11, 3.12 or 3.13.
if command -v python3 >/dev/null 2>&1; then
    exec python3 -I -B "$script_dir/scripts/setup_altera.py" "$@"
fi
printf '%s\n' 'Python was not found. Install Python 3.12 (3.11 and 3.13 also work), then run sh setup.sh again.' >&2
exit 1
