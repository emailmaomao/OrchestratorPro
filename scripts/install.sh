#!/usr/bin/env bash
#
# OrchestratorPro — install on Linux or macOS.
#
#   ./scripts/install.sh              # into ./.venv
#   ./scripts/install.sh --dev        # with the test and lint tools
#   ./scripts/install.sh --prefix ~/opt/orchestratorpro
#
# Installs into a virtual environment rather than the system Python. That is
# not caution for its own sake: OrchestratorPro pins a FastAPI range, and an
# installer that changes the Python a machine uses for everything else is an
# installer that eventually breaks something unrelated and gets blamed for it.

set -euo pipefail

PREFIX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV=""
WITH_DEV=0
QUIET=0
MIN_MAJOR=3
MIN_MINOR=11

usage() {
    cat <<'EOF'
Usage: install.sh [options]

  --prefix DIR   Install into DIR (default: the repository root)
  --venv DIR     Virtual environment location (default: PREFIX/.venv)
  --dev          Also install the test and lint tooling
  --quiet        Print less
  -h, --help     This message

Afterwards:

  <venv>/bin/orchestratorpro config check
  <venv>/bin/orchestratorpro serve
EOF
}

log() { [ "$QUIET" -eq 1 ] || printf '  %s\n' "$*"; }
step() { [ "$QUIET" -eq 1 ] || printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
        --venv)   VENV="${2:?--venv needs a directory}"; shift 2 ;;
        --dev)    WITH_DEV=1; shift ;;
        --quiet)  QUIET=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

VENV="${VENV:-$PREFIX/.venv}"

step "Checking Python"
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    for candidate in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then PYTHON="$candidate"; break; fi
    done
fi
[ -n "$PYTHON" ] || die "no Python found; install Python ${MIN_MAJOR}.${MIN_MINOR} or newer"

# Ask the interpreter rather than parsing --version: the output format has
# changed before and will again.
"$PYTHON" -c "import sys; raise SystemExit(0 if sys.version_info >= ($MIN_MAJOR, $MIN_MINOR) else 1)" \
    || die "$($PYTHON --version 2>&1) is too old; ${MIN_MAJOR}.${MIN_MINOR} or newer is required"
log "using $($PYTHON --version 2>&1) at $(command -v "$PYTHON")"

step "Checking git"
if command -v git >/dev/null 2>&1; then
    log "$(git --version)"
else
    # A warning, not a failure: the control plane records, plans, and reports
    # without git. It just cannot give an agent a worktree.
    log "WARNING: git is not installed; runs that need a worktree will fail"
fi

step "Creating the virtual environment"
if [ -d "$VENV" ]; then
    log "reusing $VENV"
else
    "$PYTHON" -m venv "$VENV" || die "could not create a virtual environment at $VENV"
    log "created $VENV"
fi

VENV_PY="$VENV/bin/python"
[ -x "$VENV_PY" ] || die "$VENV does not look like a virtual environment"

step "Installing"
"$VENV_PY" -m pip install --quiet --upgrade pip
if [ "$WITH_DEV" -eq 1 ]; then
    "$VENV_PY" -m pip install --quiet -e "$PREFIX[dev]"
    log "installed with the development extras"
else
    "$VENV_PY" -m pip install --quiet "$PREFIX"
    log "installed"
fi

step "Verifying"
"$VENV_PY" -m orchestrator.cli version >/dev/null || die "the installed package does not run"
log "$("$VENV_PY" -m orchestrator.cli version)"

if [ ! -f "$PREFIX/.env" ] && [ -f "$PREFIX/.env.example" ]; then
    cp "$PREFIX/.env.example" "$PREFIX/.env"
    log "wrote $PREFIX/.env from the example — read it before serving"
fi

step "Done"
cat <<EOF

  $VENV/bin/orchestratorpro config check
  $VENV/bin/orchestratorpro serve

  This build ships no authentication. It binds to 127.0.0.1 and refuses any
  other address unless you have configured allowed hosts and a token variable.
  Do not publish the port without an authenticating proxy in front of it.

EOF
