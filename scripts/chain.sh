#!/bin/bash
# Run a training campaign end to end after its baselines finish, then evaluate on an eval
# campaign.  Usage: scripts/chain.sh <train_campaign> <eval_campaign>
cd "$(dirname "$0")/.."
T=$1; E=$2
while pgrep -f "pramana baseline $T" >/dev/null; do sleep 60; done
python3 -m pramana all "$T" --from probe
while pgrep -f "pramana (profile|baseline) $E" >/dev/null; do sleep 60; done
python3 -m pramana.evaluate "$T" "$E" 3000
python3 -m pramana status "$T"
