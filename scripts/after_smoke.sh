#!/bin/bash
# Start seq1 once the pipeline smoke test has finished cleanly.
cd "$(dirname "$0")/.."
while pgrep -f "pramana.sequential seqsmoke3" >/dev/null; do sleep 60; done
if grep -q "sequential run finished" /tmp/seqsmoke3.out && ! grep -q Traceback /tmp/seqsmoke3.out; then
  exec python3 -m pramana.sequential seq1 eval1 \
    logicnets_jscl,rosetta_optical-flow,LU64PEEng,rosetta_spam-filter,rosetta_3d-rendering,robot_rl,vexriscv_re-place,bgm,amd_mini-isp,arm_core \
    --heldout-every 3 --budget 3000 --rounds 3 --runs-per-round 8 --discover-trials 6 --workers 4
else
  echo "smoke test failed; seq1 not started"
fi
