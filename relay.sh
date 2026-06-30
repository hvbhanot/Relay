#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
if [ "$#" -eq 0 ]; then
  set -- serve --open
fi
exec python3 -m relay.cli "$@"