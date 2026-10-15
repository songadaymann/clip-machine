#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python -m clip_machine serve "$@"
fi
exec python3 -m clip_machine serve "$@"
