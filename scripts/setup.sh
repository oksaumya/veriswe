#!/usr/bin/env bash
# VeriSWE setup: create .venv and install everything, on as many machines as possible.
#
# Strategy (first that works wins):
#   1. uv (if installed)            -> fast, and can download Python >= 3.10 itself
#   2. python3 -m venv + pip        -> using the newest Python >= 3.10 found on PATH
#   3. bootstrap uv (pip --user, then the official installer) and retry 1
set -uo pipefail

VENV="${VENV:-.venv}"
MIN_MINOR=10
log() { echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git is required but was not found on PATH"

# ---------------------------------------------------------------- pick a Python >= 3.10
find_python() {
  local c
  for c in "${PYTHON:-}" python3.13 python3.12 python3.11 python3.10 python3 python; do
    [ -n "$c" ] || continue
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_MINOR) else 1)" 2>/dev/null; then
      command -v "$c"; return 0
    fi
  done
  return 1
}
PY="$(find_python || true)"

venv_ok() { [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_MINOR) else 1)" 2>/dev/null; }

install_with_uv() {
  local uv="$1"
  log "Installing with uv ($("$uv" --version 2>/dev/null))"
  if [ -n "$PY" ]; then
    "$uv" venv -q --allow-existing --python "$PY" "$VENV" || return 1
  else
    log "No Python >= 3.$MIN_MINOR on PATH; letting uv provide Python 3.12"
    "$uv" venv -q --allow-existing --python 3.12 "$VENV" || return 1
  fi
  "$uv" pip install -q --python "$VENV/bin/python" -e . pytest
}

install_with_pip() {
  [ -n "$PY" ] || return 1
  log "Installing with $PY ($("$PY" --version 2>&1)) venv + pip"
  if ! venv_ok; then
    rm -rf "$VENV"
    "$PY" -m venv "$VENV" 2>/dev/null || { log "python -m venv is unavailable (e.g. missing python3-venv)"; rm -rf "$VENV"; return 1; }
  fi
  "$VENV/bin/python" -m pip --version >/dev/null 2>&1 || "$VENV/bin/python" -m ensurepip -q 2>/dev/null || return 1
  "$VENV/bin/python" -m pip install -q --disable-pip-version-check --upgrade pip &&
    "$VENV/bin/python" -m pip install -q --disable-pip-version-check -e . pytest
}

find_uv() {
  local c
  for c in uv "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    command -v "$c" >/dev/null 2>&1 && { command -v "$c"; return 0; }
  done
  [ -n "$PY" ] && "$PY" -m uv --version >/dev/null 2>&1 && { echo "$PY -m uv"; return 0; }
  return 1
}

bootstrap_uv() {
  log "Bootstrapping uv"
  if [ -n "$PY" ]; then
    "$PY" -m pip install -q --user uv 2>/dev/null || "$PY" -m pip install -q --user --break-system-packages uv 2>/dev/null || true
  fi
  find_uv >/dev/null && return 0
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1 || true
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1 || true
  fi
  find_uv >/dev/null
}

run_uv() { local uv; uv="$(find_uv)" || return 1; install_with_uv_cmd "$uv"; }
# `uv` may be "python -m uv" (two words), so call through a small wrapper
install_with_uv_cmd() { local cmd="$1"; uvw() { $cmd "$@"; }; install_with_uv uvw; }

ok=1
if find_uv >/dev/null; then
  run_uv && ok=0
fi
if [ $ok -ne 0 ]; then
  install_with_pip && ok=0
fi
if [ $ok -ne 0 ]; then
  bootstrap_uv && run_uv && ok=0
fi
[ $ok -eq 0 ] || die "could not create the Python environment. Install Python >= 3.$MIN_MINOR (with venv) or uv, then re-run 'make setup'."

"$VENV/bin/python" -c "import veriswe.cli, litellm, textual" || die "installation finished but VeriSWE failed to import"
log "setup OK ($("$VENV/bin/python" --version 2>&1) in $VENV)"
