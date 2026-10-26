# dev1: first end-to-end campaign

Train: 8 designs, 6 families (vexriscv_re-place, amd_mini-isp, arm_core, robot_rl, bgm,
rosetta_3d-rendering, logicnets_jscl, rosetta_spam-filter). Part xcvu3p-ffvc1517-2-e, Vivado
2025.2, 4 threads per run. Held out: eval1 (corescore_500_mod, finn_radioml,
rosetta_digit-recognition, LU32PEEng, bnn).

## Cost
- 430 Vivado runs: 56 baselines, 53 probes, 192 search runs (exploit/test/audit), 41 discovery
  trials, 88 confirm runs (replay, transfer, recipe transfer).
- About 12 h wall time on a shared machine (another user held ~75 GB RAM).
- LLM: $5.60 total (prune/re-prune $3.91, checkpoint reports $0.29, discovery agents $1.39).

## Best gain per design (ΔWNS ns vs the input DCP)
| design | best | how |
|--|--|--|
| vexriscv_re-place | +1.053 | re-place, then phys_opt |
| amd_mini-isp | +0.796 | re-place |
| logicnets_jscl | +0.535 | place Default > phys_opt Explore, then post-route phys_opt |
| robot_rl | +0.432 | re-place |
| rosetta_3d-rendering | +0.383 | re-place > phys_opt, then further steps (found in confirm) |
| bgm | +0.351 | post-route phys_opt chains only (best found in confirm) |
| arm_core | +0.195 | post-route phys_opt chain only |
| rosetta_spam-filter | +0.091 | re-place > phys_opt > routing |

## What we learned about the designs
- Two kinds of design. Most gain from a fresh placement (place Default then phys_opt Explore).
  arm_core and bgm lose 0.08–0.38 ns from re-placing; only post-route phys_opt helps them.
- Route-only steps from the input are usually negative (6 of 8 designs).
- opt mostly hurts or does nothing; when it "helps" it's the re-place it forces.
- Returns fall fast: after round 1 most runs from the best state gain exactly 0.
- phys_opt's pass table is a useful mid-run signal: when it optimised nothing (40 of 85 cases)
  no later phys_opt on that state gained (0 of 40).

## What we learned about the framework
- **LLM pruning works; LLM ranking doesn't.** Audits of pruned-out factors didn't find anything
  the prune missed. But the LLM's predicted ΔWNS correlated with measured ΔWNS at 0.52 after
  round 1, then 0.01, then −0.15. Its weight in allocation now equals that measured correlation.
- **Discovery found small, design-specific gains.** Agents wrote reasonable recipes (temporary
  clock over-constraint + phys_opt loops, retiming + critical-cell opt); best +0.065 (bgm),
  +0.046 (rosetta_3d). Transferred to other designs they gave ~0 (they start from states where
  phys_opt is already saturated). None became general knowledge.
- **No reliable feature conditions with 8 designs.** 7 conditions tested; all failed the shuffle
  test, so the policy is one bin. The best two-bin split (logic_frac) scored 0.884 of oracle on
  held-out families vs 0.872 for one bin, within noise.
- **One-bin policy:** place Default > phys_opt Explore, then phys_opt AddRetime > route Explore,
  then route AggressiveExplore, each keep-if-better. Replayed on held-out training families it
  reaches 0.872 of each design's best result.
- **Infrastructure:** earlyoom on this machine kills the largest process below ~10% free memory;
  that caused all "crash" results (26 runs). Fixed with a memory gate shared across processes,
  crash retries, and fewer workers.
- Vivado is deterministic for a fixed thread count, so identical repeats give zero noise. Repeats
  now use a different thread count.

## Fixes made after dev1 (active from dev2)
- Post-route phys_opt options Vivado rejects are excluded (probe + chain_ok).
- Beam-2 exploit slot; per-command cap; bandit stats split by start kind.
- place_default control baseline (separates opt's effect from its forced re-place).
- Crash retry; shared memory gate; thread-perturbed repeats for noise.
- Discovery agents stay in their own directory (one wrote files into the repo root).
- Weak conditions graded D.

## Evaluation on eval1 (5 unseen designs, 3000 s budget, ΔWNS ns vs input)
| design | dev1 policy | best single Explore baseline |
|--|--|--|
| corescore_500_mod | +0.477 | +0.099 (place Default > phys_opt Explore) |
| finn_radioml | +0.646 | +0.563 (opt Explore / place Default) |
| rosetta_digit-recognition | +0.378 | +0.111 (place Default > phys_opt Explore) |
| LU32PEEng | +3.314 | +2.606 (place Default > phys_opt Explore) |
| bnn | +0.258 | +0.233 (place Default > phys_opt Explore) |

All valid. Time check: running all 6 Explore baselines and keeping the best takes 38–53 min
in total; the policy takes 41–67 min in one session and beats that best-of-6 on 5/5 designs
(by +0.38, +0.08, +0.27, +0.71, +0.03 ns). The bnn and finn margins are small and the policy
used 4–14 min more there; a strictly time-matched comparison is still to do.
