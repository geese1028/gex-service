#!/bin/zsh
# Create/refresh the service venv. Uses uv when available, otherwise the first
# Python >= 3.11 it can find (Homebrew paths, or BASE_VENV's interpreter).
#
#   deploy/bootstrap_venv.sh                         # create .venv and install the package
#   PYTHON=/path/to/python3.12 deploy/bootstrap_venv.sh
#   BASE_VENV=/path/to/other/.venv deploy/bootstrap_venv.sh   # reuse that venv's base interpreter

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

find_python() {
  if [[ -n "${PYTHON:-}" ]]; then echo "$PYTHON"; return; fi
  local candidates=(
    /opt/homebrew/bin/python3.12
    /opt/homebrew/bin/python3.13
    /opt/homebrew/bin/python3.11
    /usr/local/bin/python3.12
  )
  # Base interpreter of another existing venv, if BASE_VENV is given.
  local cfg="${BASE_VENV:-}/pyvenv.cfg"
  if [[ -n "${BASE_VENV:-}" && -f "$cfg" ]]; then
    local home
    home=$(grep -E '^home *= *' "$cfg" | sed -E 's/^home *= *//')
    [[ -n "$home" && -x "$home/python3" ]] && candidates=("$home/python3" "${candidates[@]}")
  fi
  for c in "${candidates[@]}"; do
    if [[ -x "$c" ]] && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
      echo "$c"; return
    fi
  done
  echo "no Python >= 3.11 found; set PYTHON=/path/to/python3" >&2
  exit 1
}

if command -v uv >/dev/null 2>&1; then
  uv sync --no-dev
  exit 0
fi

PY=$(find_python)
echo "using $PY ($($PY --version))"
if [[ ! -x .venv/bin/python ]]; then
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e .
echo "venv ready: $PROJECT_ROOT/.venv"
