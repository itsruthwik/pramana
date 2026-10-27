#!/bin/bash
# 1. wait for the pipeline smoke test; stop if it failed
# 2. placer sweep (reference runs, shared store) for the 10 training designs, via dev2
# 3. in parallel: seq1, and the dev2 portfolio test on eval1 (all within the 4-Vivado cap)
cd "$(dirname "$0")/.."
while pgrep -f "pramana.sequential seqsmoke3" >/dev/null; do sleep 60; done
if ! grep -q "sequential run finished" /tmp/seqsmoke3.out || grep -q Traceback /tmp/seqsmoke3.out; then
  echo "smoke test failed; nothing started"; exit 1
fi
python3 -m pramana baseline dev2 > campaigns/dev2/sweep.out 2>&1
python3 -m pramana.sequential seq1 eval1 \
  logicnets_jscl,rosetta_optical-flow,LU64PEEng,rosetta_spam-filter,rosetta_3d-rendering,robot_rl,vexriscv_re-place,bgm,amd_mini-isp,arm_core \
  --heldout-every 3 --budget 3000 --rounds 3 --runs-per-round 8 --discover-trials 6 --workers 4 \
  > campaigns/seq1.out 2>&1 &
[ -d campaigns/dev2/out_pre_portfolio ] || cp -r campaigns/dev2/out campaigns/dev2/out_pre_portfolio
cp campaigns/dev2/kb/kb.json campaigns/dev2/kb/kb_pre_portfolio.json
python3 -m pramana distill dev2 >> campaigns/dev2/sweep.out 2>&1
python3 -m pramana compile dev2 >> campaigns/dev2/sweep.out 2>&1
cp -r campaigns/dev2/out campaigns/dev2/out_portfolio
python3 -c "
from pramana.config import Config; from pramana.campaign import Campaign; from pramana import evaluate
evaluate.run(Campaign(Config.load('dev2')), Campaign(Config.load('eval1')), 3000,
             bundle='campaigns/dev2/out_portfolio/mithya', label='dev2_portfolio')
" >> campaigns/dev2/sweep.out 2>&1
wait
