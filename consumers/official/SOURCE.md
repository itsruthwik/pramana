# Official FPL'26 contest agent (vendored copy for pramana evaluation)

Source: https://github.com/Xilinx/fpl26_optimization_contest, commit
`0a5803409d3e70e148316b4a8f4881188d9c5de8` ("final benchmarks (#51)"), read from the `xilinx`
remote of `/mnt/vault1/kmhatre/workspace/FPL_26_contest_asu` with `git show xilinx/main:<path>`.

Copied: `dcp_optimizer.py`, `SYSTEM_PROMPT.TXT`, `requirements.txt`, `LICENSE-APACHE-2.0.txt`,
`VivadoMCP/{vivado_mcp_server.py,requirements.txt}`,
`RapidWrightMCP/{server.py,rapidwright_tools.py,requirements.txt}`. No RapidWright checkout:
`pramana/official_eval.py` points `RAPIDWRIGHT_PATH`/`CLASSPATH` at a built one.

Unchanged byte-for-byte: everything except `dcp_optimizer.py` and
`RapidWrightMCP/rapidwright_tools.py`. New file: `SYSTEM_PROMPT_PROTOCOL.TXT`. All edits are
marked `[pramana]`.

## Knowledge vs protocol in the upstream prompt

`SYSTEM_PROMPT.TXT` (83 lines) is formatted with `temp_dir` and `input_dcp`.

| Part | Kind | none / pramana |
|---|---|---|
| Preamble, CURRENT STATE | protocol | kept (dropped the "look for Use PBLOCK strategy" hint) |
| OPTIMIZATION STRATEGIES 1-3 (fanout split rule, pblock workflow with thresholds, phys_opt advice) | knowledge | dropped |
| WORKFLOW 1-3 (strategy selection by spread/fanout; pblock steps; fanout steps) | knowledge | replaced by a neutral loop: try, measure, keep or revert |
| WORKFLOW 3 mechanics (RapidWright edit -> intermediate DCP -> open in Vivado -> route), 4-5 (iterate, save) | protocol | kept |
| TOOLS | protocol | kept |
| CRITICAL RULES | protocol | kept, minus the split-factor rule (knowledge) |
| Initial analysis `RECOMMENDATION: Use PBLOCK strategy` line | knowledge (hand threshold) | stripped; the spread numbers stay |

`SYSTEM_PROMPT_PROTOCOL.TXT` is the result. It has the same two format fields.

## Changes to the copy

1. `dcp_optimizer.py`
   - `KB_VARIANTS`, `_kb_variant()`, `_kb_dir()`: variant and bundle in env vars `OFFICIAL_KB`,
     `OFFICIAL_KB_DIR` (set from the CLI).
   - `load_system_prompt_parts()`: `hand` -> upstream prompt unchanged; `none` -> protocol
     template + one line saying no knowledge is given; `pramana` -> protocol template +
     `::pramana::` proc reference + `<kb-dir>/context.md`. The appendix is never `.format()`ted.
   - `_strip_hand_hints()`: removes the RECOMMENDATION line from the initial analysis for
     none/pramana.
   - `_pramana_source_recipes()`: for `pramana`, sources `<kb-dir>/recipes.tcl` through the
     Vivado MCP `run_tcl` tool after every `vivado_open_checkpoint` (the server runs
     `close_design` + `open_checkpoint` in one persistent Vivado, so procs survive, but this keeps
     it robust) and sets `::pramana::deadline` from the time budget.
   - Time budget (`--time-budget-s`, all variants): when exceeded, one final user turn asks the
     model to save the best routed design to the output path, then the loop stops. Upstream has
     no budget; the contest harness kills it at 1 h.
   - CLI: `--kb {hand,none,pramana}` (default hand), `--kb-dir`, `--run-dir`, `--time-budget-s`,
     `--base-url`, `--dry-run-prompt OUT_DIR` (writes `system_prompt.<kb>.txt/.json`, no servers,
     no LLM).
   - `OpenAI(base_url=...)` reads `OFFICIAL_BASE_URL` (default OpenRouter, as upstream).
2. `RapidWrightMCP/rapidwright_tools.py`: an explicit `RAPIDWRIGHT_PATH` with a `jars/` dir wins
   over the empty submodule location.

Everything else (tool loop, best-WNS tracking, token accounting, `token_usage.json`) is upstream.
Note that upstream never banks a best checkpoint: the output DCP is whatever the model writes.
`pramana/official_eval.py` therefore measures the output independently (`pramana.measure`).
