# pramana

Generate a timing-closure knowledge base from a training set of post-route Vivado DCPs, and
compile it for an LLM agent (maya) or a deterministic flow (mithya).

What and why: [docs/framework_arch.md](docs/framework_arch.md).

## Stages

| stage | command | what |
|--|--|--|
| S0 | `space` | search space parsed from Vivado `help` (specs/vivado_2025.2) |
| S0 | `probe` | drop directives/values that don't run on this part |
| S1 | `profile` | read-only reports, top paths, features |
| S2 | `baseline` | Explore per command + a repeat (controls, evidence, noise) |
| S3 | `prune` | LLM prunes each design's space (cuts = hypotheses) |
| S4 | `search` | rounds: bandit + hypothesis tests + audits, re-prune, checkpoint report |
| S5 | `discover` | Claude Code agent writes custom Tcl recipes per design |
| S6 | `confirm` | replay best chains from scratch; transfer to every other design |
| S7 | `distill` | KB with LOFO-checked conditions, policies, bins, pruning rules |
| S8 | `compile` | `out/maya/` and `out/mithya/` bundles |

```bash
python3 -m pramana init dev1 --designs vexriscv_re-place,amd_mini-isp --workers 8
python3 -m pramana all dev1                    # S0..S8
python3 -m pramana status dev1
python3 -m pramana.evaluate dev1 eval1 3000     # apply the mithya bundle to eval1's designs
scripts/chain.sh dev1 eval1                    # all of the above, unattended
```

Campaign data lives in `campaigns/<name>/` (git-ignored): `results.jsonl` (every run),
`runs/<design>/<run_id>/` (Tcl, logs, checkpoints), `prune/`, `llm/` (every prompt and
response), `reports/` (checkpoint reports), `kb/kb.json`, `out/`.

## Settings

- Vivado 2025.2 (`pramana/config.py`); fixed threads per run so run times are comparable.
- LLM: Claude Code headless (`claude -p`), Sonnet 5.5, effort low, on the user's subscription.
- A result counts only if fully routed with 0 route errors, WHS ≥ 0 and WPWS ≥ 0.
- Unit: ΔWNS in ns against the run's start state.

Tests: `python3 -c "import tests.test_core as t; [getattr(t,n)() for n in dir(t) if n.startswith('test_')]"`
