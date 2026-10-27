# Evaluating maya with a knowledge base: `pramana.maya_eval`

`pramana/maya_eval.py` runs the maya LLM agent (`consumers/maya`, a patched copy of
FPL_contest at `14a36fb`; see `consumers/maya/SOURCE.md`) on one DCP with one of three
knowledge variants. It returns the final result as a dict.

| `--kb` | system prompt | Tcl sourced in the session |
|---|---|---|
| `none` | `SYSTEM_PROMPT_PROTOCOL.TXT` (contest rules + harness/script protocol) + the keep-best guard reference | `::opt::` library (only the guard is documented) |
| `hand` | maya's original prompt, unchanged: `SYSTEM_PROMPT.TXT` + `::opt::` recipe reference | `::opt::` library |
| `pramana` | the `none` prompt + a `::pramana::` proc reference + `<bundle>/context.md` | `::opt::` library + `<bundle>/recipes.tcl` |

In `none` and `pramana`, the hand-written `=>` derived reads and the large-design advice are
removed from the initial analysis, and the plateau nudge is neutral. `hand` is byte-identical
to the original. See SOURCE.md for which parts of the prompt count as knowledge and which as
protocol.

## What you must provide

- **An OpenAI-compatible endpoint**: `--provider` is one of maya's names (`openrouter`,
  `advent`, `asu_rc`, `nvidia`) or a base URL such as `http://host:11434/v1`.
- **A model id**: `--model`. Without it you get the provider's default in `maya.py`, e.g.
  `deepseek/deepseek-v4-pro` on openrouter.
- **An API key in an env var**: `--key-env NAME`. The default is `OPENROUTER_API_KEY` for
  openrouter and `LLM_API_KEY` otherwise. The key is passed to maya by env-var name, never on
  a command line.
- **A Python with maya's requirements**: `pip install -r consumers/maya/requirements.txt`
  installs mcp, openai, rapidwright and pexpect. Pass it with `--python` or `$MAYA_PYTHON`. The
  system python3 does not have them.
- **Vivado**: `pramana.config.VIVADO_SETTINGS` is sourced. JAVA_HOME defaults to Vivado's
  bundled JRE.
- **RapidWright**: a built checkout. The default is FPL_contest's, override it with
  `$MAYA_RAPIDWRIGHT_PATH`.
- For `pramana`: a compiled maya bundle (`campaigns/<c>/out/maya/`, containing `context.md` and
  `recipes.tcl`).

## Run

```bash
# check the prompt only: no Vivado, no LLM, no key needed
python3 -m pramana.maya_eval --dcp D.dcp --out /tmp/me --kb pramana \
    --bundle campaigns/dev2/out/maya --python /path/to/venv/bin/python --dry-run

# real run (one Vivado slot is held for the whole run)
export OPENROUTER_API_KEY=...
python3 -m pramana.maya_eval --dcp D.dcp --out runs/maya/D.pramana --kb pramana \
    --bundle campaigns/dev2/out/maya --budget-s 3600 \
    --provider openrouter --model deepseek/deepseek-v4-pro --python /path/to/venv/bin/python
```

From Python: `maya_eval.run(bundle_dir, dcp, out_dir, kb_variant, budget_s, provider, model,
key_env=..., python=...)`.

Output in `--out`: `maya.log` (maya's stdout and stderr), `run/` (maya's run dir:
`token_usage.json`, `attempts.jsonl`, `llm_prompts/`, plans, Vivado logs), `output.dcp`, and
`result.json`. The result fields:

- `valid`: maya's ship gate (routed, 0 route errors, WHS >= 0, WPWS >= 0) held on every banked
  design, and the output exists. This is not an independent re-measurement.
- `input_wns`, `final_wns` (best banked attempt, else the input), `d_wns`, `maya_best_wns`.
- `secs`, `maya_runtime_s`.
- `llm_calls`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `cached_tokens`,
  `cost_usd`.
- `attempts`, `banked`, `exit_code`, `timed_out`.

## Concurrency and limits

- maya starts Vivado itself, so `run()` holds one of the machine-wide slots in
  `/tmp/pramana_vivado_slots` (`pramana.vivado.vivado_slot`) and waits for free memory. At
  most 4 Vivado runs execute at once.
- maya stops itself at `budget_s`. The subprocess is killed `budget_s + 900` s after it starts.
- maya's own caps still apply: 60 LLM calls and $0.85 of OpenRouter-reported cost.

## Comparing variants

Run the same DCP, model and budget under each of `none`, `hand` and `pramana`, then compare
`d_wns`, `valid`, `total_tokens` and `cost_usd`. LLM runs are not deterministic, so repeat each
cell.
