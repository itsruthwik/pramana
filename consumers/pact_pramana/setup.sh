#!/bin/bash
# Recreate consumers/pact (git-ignored) from the local PACT clone at tag v2.0.0 and apply the
# pramana patch. See SOURCE.md.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
SRC=${PACT_SRC:-/mnt/vault1/kmhatre/workspace/FPL_26_contest_asu/data/PACT}
RW=${PACT_RAPIDWRIGHT:-/mnt/vault1/kmhatre/workspace/FPL_26_contest_asu/contest_winners/4_Fu_Do_Fdo/RapidWright}
COMMIT=4081062
DST=$REPO/consumers/pact
rm -rf "$DST"; mkdir -p "$DST"
git -C "$SRC" archive "$COMMIT" FDAgents VivadoMCP RapidWrightMCP knowledge validate_dcps.py \
    pyproject.toml Makefile LICENSE LICENSE-APACHE-2.0.txt README.md .env.example .python-version \
    requirements.txt | tar -x -C "$DST"
ln -sfn "$RW" "$DST/RapidWright"
patch -p1 -d "$DST" < "$HERE/pact_pramana.patch"
echo "consumers/pact ready (PACT $COMMIT + pramana patch); RapidWright -> $RW"
