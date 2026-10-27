# pramana

Generate a timing-closure knowledge base from a training set of post-route Vivado DCPs, and
compile it for an LLM agent (maya) or a deterministic flow (mithya).

What and why: [docs/framework_arch.md](docs/framework_arch.md).

## Stages

Framework-level (`pramana prepare <campaign>`): done once per Vivado version, part or DCP and
shared by all campaigns; anything already in the store is skipped.

| stage | command | what | stored in |
|--|--|--|--|
| S0 | `space` | search space parsed from Vivado `help` | `specs/vivado_<ver>/<part>/space.json` |
| S0 | `probe` | drop directives/values that don't run on this part | same dir (committed) |
| S1 | `profile` | read-only reports, top paths, features | `store/dcp/<id>-<md5>/vivado_<ver>/profile/` |
| S2 | `baseline` | Explore per command + repeats (controls, evidence) | `store/vivado_<ver>/t<threads>/` |

Campaign-level (`pramana all <campaign>` runs prepare first, then these):

| stage | command | what |
|--|--|--|
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

Shared data lives in `store/` (git-ignored); campaigns read reference runs and profiles from it
(matched by DCP md5). `scripts/migrate_to_store.py` moved older campaigns' copies there.
Campaign data lives in `campaigns/<name>/` (git-ignored): `results.jsonl` (every run),
`runs/<design>/<run_id>/` (Tcl, logs, checkpoints), `prune/`, `llm/` (every prompt and
response), `reports/` (checkpoint reports), `kb/kb.json`, `out/`.

## Settings

- Vivado 2025.2 (`pramana/config.py`); fixed threads per run so run times are comparable.
- LLM: regular Claude Code sessions (Sonnet 5.5, effort low, user subscription), one tmux session each (`tmux ls`, `tmux attach -t pr-<campaign>-<stage>-<name>`); saved like any session (`claude --resume`). The analyst is one session per design, resumed every round.
- A result counts only if fully routed with 0 route errors, WHS ≥ 0 and WPWS ≥ 0.
- Unit: ΔWNS in ns against the run's start state.

Tests: `python3 -c "import tests.test_core as t; [getattr(t,n)() for n in dir(t) if n.startswith('test_')]"`
