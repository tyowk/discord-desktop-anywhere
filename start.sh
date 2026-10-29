#!/usr/bin/env bash
# Entry point for Replit and plain Linux. All logic lives in the `dda` Python package;
# this wrapper only finds Python and forwards the command (default: run).
#
#   bash start.sh            start everything
#   bash start.sh doctor     check the host for missing dependencies
#   bash start.sh password   print the login password
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON="$candidate"
      break
    fi
  done
fi
if [ -z "$PYTHON" ]; then
  echo "Python 3.8+ is required but was not found. Install python3 (see README)." >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  echo "Python 3.8 or newer is required (found: $("$PYTHON" --version 2>&1))." >&2
  exit 1
fi

exec "$PYTHON" -m dda "${@:-run}"
