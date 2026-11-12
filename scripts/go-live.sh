#!/usr/bin/env bash
#
# go-live.sh — safe pre-flight for the Maya agent.
#
# Runs the gates you should never skip before risking capital, in order, and
# only starts the agent if they pass (or you explicitly override):
#   1. .env present
#   2. venue connectivity (python -m maya --check)
#   3. backtest go/no-go gate on real history
#   4. explicit confirmation for mainnet (live) mode
#
# Usage:
#   scripts/go-live.sh                 # uses .env
#   BACKTEST_DAYS=180 scripts/go-live.sh
#   PYTHON=python3.13 scripts/go-live.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
DAYS="${BACKTEST_DAYS:-365}"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
die() { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

# 1. config -----------------------------------------------------------------
say "1/4  config"
[ -f .env ] || die "No .env found. Copy .env.example to .env and configure it (see docs/HYPERLIQUID.md)."
MODE="$(grep -E '^MAYA_MODE=' .env | tail -1 | cut -d= -f2 | tr -d '[:space:]' || true)"
MODE="${MODE:-sim}"
echo "mode: $MODE"

# 2. connectivity -----------------------------------------------------------
say "2/4  venue connectivity"
if [ "$MODE" != "sim" ]; then
  $PY -m maya --check || die "Connectivity check failed — fix credentials/network before trading."
else
  echo "sim mode — skipping connectivity check."
fi

# 3. backtest gate ----------------------------------------------------------
say "3/4  backtest go/no-go gate (${DAYS}d)"
SRC="hyperliquid"
grep -qiE '^MAYA_VENUE=binance' .env && SRC="ccxt"
set +e
OUT="$($PY -m maya.backtest --source "$SRC" --days "$DAYS" 2>&1)"
set -e
echo "$OUT" | tail -n 22
if echo "$OUT" | grep -q "VERDICT: NO-GO"; then
  printf '\n\033[33m⚠  Backtest verdict: NO-GO — the strategy did not pass the go/no-go gates.\033[0m\n'
  read -r -p "Start the agent anyway? [y/N] " ans
  [ "${ans:-N}" = "y" ] || die "Aborted. Refine the strategy or data before going live."
elif echo "$OUT" | grep -q "VERDICT: GO"; then
  echo "Backtest gate: GO."
else
  printf '\n\033[33m⚠  Could not read a backtest verdict (data/network issue?). Review the output above.\033[0m\n'
  read -r -p "Continue anyway? [y/N] " ans
  [ "${ans:-N}" = "y" ] || die "Aborted."
fi

# 4. live confirmation ------------------------------------------------------
say "4/4  start"
if [ "$MODE" = "live" ]; then
  printf '\033[31mLIVE (mainnet, REAL MONEY) mode selected.\033[0m\n'
  read -r -p "Type GO LIVE to start trading real funds: " confirm
  [ "$confirm" = "GO LIVE" ] || die "Aborted."
fi

echo "Starting agent (Ctrl-C to stop)…"
exec $PY -m maya
