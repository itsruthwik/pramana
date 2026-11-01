# Evaluating three agents with one pramana knowledge base

Three agents consume the same compiled KB from a campaign (`campaigns/<c>/out/`): the official
FPL'26 contest agent (the baseline), maya, and PACT. Each has a driver under `pramana/` that
runs it on one DCP with one knowledge variant, holds a machine-wide Vivado slot, and re-measures
the input and output checkpoints with a fresh Vivado (`pramana.measure`) so all three are scored
the same way: ΔWNS in ns on the contest clock, valid only if fully routed, 0 route errors,
WHS ≥ 0, WPWS ≥ 0.

| agent | vendored copy | driver | `--kb` variants | what "pramana" injects |
|---|---|---|---|---|
| official contest agent (Xilinx/fpl26_optimization_contest @ 0a58034) | `consumers/official/` ([SOURCE.md](../consumers/official/SOURCE.md)) | `pramana.official_eval` | hand, none, pramana | protocol prompt + `::pramana::` reference + `context.md`; `recipes.tcl` sourced via `run_tcl` after every `open_checkpoint` |
| maya (FPL_contest @ 14a36fb) | `consumers/maya/` ([SOURCE.md](../consumers/maya/SOURCE.md)) | `pramana.maya_eval` | none, hand, pramana | protocol prompt + `::pramana::` reference + `context.md`; `recipes.tcl` sourced |
| PACT / FDAgents v2.0.0 (@ 4081062) | `consumers/pact/` (git-ignored; [pact_pramana/SOURCE.md](../consumers/pact_pramana/SOURCE.md)) | `pramana.pact_eval` | none, hand, pramana | `out/adapters/pact/cases.yaml` as the case corpus; `context.md` + matched cases as an advisory `learned_prior` in the Decision LLM's state. No Tcl: PACT's loader rejects it |

The same KB files feed all three: `out/maya/context.md` and `out/maya/recipes.tcl` directly for
the official agent and maya; for PACT the adapter (`pramana.adapters.write_pact_cases`) rewrites
the KB into PACT's case schema and copies `context.md` next to it.

## Why "hand" differs per agent

Each agent's baseline knowledge is whatever it shipped with: the official agent's strategy prompt
(fanout split, pblock thresholds), maya's SYSTEM_PROMPT and `::opt::` recipe reference, PACT's
`knowledge/cases.yaml` and skills guide. `none` strips that knowledge and keeps the tool
protocol. `pramana` replaces it with the learned KB. The split between knowledge and protocol
is documented per agent in its SOURCE.md.

## Running

Same model for all three (any OpenRouter model with tool calling and the Responses API; checked
for `deepseek/deepseek-v4-pro` and `deepseek/deepseek-v4-flash`). Output dirs are
`<design>.<kb>[.<tag>]`; the pro runs have no tag, the flash rerun uses `.flash`. Key in `~/.config/pramana/llm.env` as `OPENROUTER_API_KEY`.

```bash
source ~/.config/pramana/llm.env
export MAYA_RAPIDWRIGHT_PATH=/mnt/vault1/kmhatre/workspace/FPL_26_contest_asu/RapidWright   # built checkout
M=deepseek/deepseek-v4-pro; D=fpl26_contest_benchmarks/vexriscv_re-place_2025.1.dcp
python3 -m pramana.official_eval --dcp $D --out runs/official/vex.pramana --kb pramana --bundle campaigns/dev1/out/maya --model $M
python3 -m pramana.maya_eval     --dcp $D --out runs/maya/vex.pramana     --kb pramana --bundle campaigns/dev1/out/maya --model $M --provider openrouter
python3 -m pramana.pact_eval     --dcp $D --out runs/pact/vex.pramana     --kb pramana --bundle campaigns/dev1/out/adapters/pact --model $M
```

`scripts/agent_eval_table.py <campaign>` collects every `result.json` into one markdown table
(dev1's is at `campaigns/dev1/agent_eval_summary.md`).

Each run writes `result.json` with the agent's own bookkeeping (`agent_*`, `maya_*`, `pact_*`
fields, LLM calls, tokens, cost) and the independent measurement (`input_wns`, `output_wns`,
`output_valid`, `final_wns`, `d_wns`, `valid`). `final_wns` is the valid output, else the
untouched input (what the contest would score). At most 4 Vivado processes run machine-wide;
each agent run holds one slot for its whole budget plus a 15 min grace.

Prerequisites: Vivado 2025.2 (`pramana/config.py`), a built RapidWright, Java 11 (PACT's and the
official agent's servers), the system python3 with `mcp openai pexpect` for maya and the official
agent, and PACT's own Python 3.13 venv (`pramana/pact_eval.py` `PACT_PYTHON`).

## Caveats

- Outputs are re-measured with the input's clock constraints re-imposed (`pramana.measure`
  `clocks=`). PACT's `clock_tighten` skill rewrites the contest clock's period and reports WNS
  against the new period: on vexriscv it left the checkpoint at 1.986 ns and banked -0.103,
  which is -0.519 at the real 1.57 ns. An agent's self-reported WNS is never used for scoring.
- The official agent stops after 50 iterations (its upstream safety limit) and exits 1 with
  "did not complete successfully" even though the output checkpoint was written. A fast model
  hits this before the hour (flash on amd_mini-isp: 50 iterations in 29 min); the independent
  measurement still scores the output.
- The official agent never banks a best checkpoint; its output is whatever the model writes
  before the budget's final turn. The independent measurement catches an unrouted or worse
  output, and `final_wns` then falls back to the input.
- PACT v2.0.0 is a sanitized release: about 20 scripts its code calls are missing, so some
  skills skip. Its default config also switches case matching off; the hand/pramana variants
  restore the loader's own thresholds.
- LLM runs are not deterministic. Repeat cells before comparing.
