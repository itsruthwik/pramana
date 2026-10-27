# maya (vendored copy for pramana evaluation)

Source: `/mnt/vault0/rsunketa/FPL_contest`, commit `14a36fbf2e9c78e51939f0ec3d899455d9a8ad73`
("Plateau streak ignores failed attempts"). Every file was taken with `git show HEAD:<path>`;
none of them had uncommitted changes. No `*.pem`, no RapidWright checkout (232 MB), no run dirs.

Copied: `maya.py`, `vivado_parse.py`, `plan_script.py`, `maya_dev_modes.py` (imported by
`main()`), `SYSTEM_PROMPT.TXT`, `context/{opt_recipe_context.md,vivado_opt_recipes.tcl,
vivado_opt_postroute.tcl}`, `VivadoMCP/{vivado_mcp_server.py,requirements.txt}`,
`RapidWrightMCP/{server.py,rapidwright_tools.py,requirements.txt}`, `requirements.txt`,
`LICENSE-APACHE-2.0.txt`.

Unchanged byte-for-byte: everything except `maya.py` and `plan_script.py`. New file:
`SYSTEM_PROMPT_PROTOCOL.TXT`. All edits are marked `[pramana]` in the code.

RapidWright is not copied. maya uses `$RAPIDWRIGHT_PATH`/`$CLASSPATH` when set, and
`pramana/maya_eval.py` points them at the built checkout in FPL_contest (read-only use).

## Knowledge vs protocol in maya's prompt

maya's system prompt = `SYSTEM_PROMPT.TXT` (formatted) + the `::opt::` reference
(`context/opt_recipe_context.md` with its "Diagnosis & targeting" and "Typical agentic loop"
sections removed, plus a header). The first user message carries the initial analysis from
`vivado_parse.render_analysis`.

| Part | Kind | none / pramana |
|---|---|---|
| Preamble, §1 objective/scoring/validity/latency rule, "fix hold first" | protocol (contest rules) | kept |
| §2 what the harness measures and when it refreshes | protocol | kept; dropped the paragraph that ranks "=>" reads against §5–7 |
| §3 cost of each lever: measure the first P&R, one re-place in the first script, sweep basins across turns | knowledge (strategy) | dropped |
| §3 watchdog + "nothing banks until your script returns" | protocol | kept (new §3) |
| §4 signal → meaning → lever table | knowledge | dropped |
| §5 decision procedure (COMMIT/MEASURE/SKIP, AggressiveExplore route, basin sweep, cone re-place, pivot rules) | knowledge | dropped |
| §6 lever reference (directive lists, `-unplace` first, phys_opt position, opt_design skip, S1/S2) | knowledge | dropped |
| §7 negative knowledge, xcvu3p dead options, traps | knowledge | dropped |
| §8 one Tcl script per reply, what the harness does with it, script rules (guard, final unwrapped route, no checkpoint I/O, exact names) | protocol | kept (new §4); dropped "first script = a single re-place" and the "`-unplace` is REQUIRED" rule |
| §9 operating rules | protocol | kept (new §5); the "pivot class, see §5" bullet became the determinism/duplicate-refusal fact |
| Authoring note | maintainer note | dropped |
| `::opt::` reference: `try_keep_if_better` guard | protocol (the harness validator expects it) | kept as a short guard-only reference |
| `::opt::` reference: fix tables, recipe menu | knowledge | dropped (library still sourced, see below) |
| Initial analysis: facts (`·` lines, numbers, reports) | facts | kept |
| Initial analysis: `=>` derived reads + their legend; LARGE DESIGN advice (first-script recipe) | knowledge (hand-written in `vivado_parse`) | stripped; the factual LARGE DESIGN marker stays |
| `[PLATEAU]` panel (lever-class pivot advice, names `replace_critical_cone`) | knowledge | reduced to "N attempts in a row banked no gain." |

`SYSTEM_PROMPT_PROTOCOL.TXT` is SYSTEM_PROMPT.TXT with the knowledge parts above removed and
sections renumbered. Nothing in it was reworded beyond cross-references. It has no braces, so it
formats like the original.

## Changes to the copy

1. `SYSTEM_PROMPT_PROTOCOL.TXT` (new): the protocol-only template (table above).
2. `maya.py`:
   - `KB_VARIANTS`, `_kb_variant()`, `_kb_dir()`: the variant lives in env vars `MAYA_KB` /
     `MAYA_KB_DIR` (set from the CLI) so `maya_dev_modes`' re-import of `maya` agrees.
   - `_kb_prompt_parts()`: for none/pramana, (template = PROTOCOL file, appendix = guard
     reference [+ `::pramana::` proc reference + `<kb-dir>/context.md` for pramana]). The
     appendix is never `.format()`ted. none adds one line saying no prior knowledge is given.
   - `load_system_prompt_parts()`: dispatches to `_kb_prompt_parts()` unless `--kb hand`.
     hand is byte-identical to the original prompt (checked).
   - `_strip_derived_reads()` applied to the initial analysis for none/pramana.
   - `_plateau_panel()`: knowledge-neutral one-liner for none/pramana.
   - `_ensure_recipes_loaded()`: for pramana, also sources `<kb-dir>/recipes.tcl` (idempotent,
     probes `::pramana::keep_if_better`) and sets `::pramana::deadline` to maya's optimisation
     deadline. A failed source marks recipes as not loaded, as for `::opt::`.
   - CLI: `--kb {none,hand,pramana}` (default hand), `--kb-dir <bundle>`,
     `--provider {openrouter,advent,asu_rc,nvidia}` (picks that provider's base URL, and its
     default model unless `--model` is given), `--dry-run-prompt OUT_DIR`.
   - `dry_run_prompt()`: builds the prompt the way `_optimize_body` does (recipes sourced) and
     writes `system_prompt.<kb>.txt` + `.json` manifest. Starts no Vivado and makes no LLM call.
3. `plan_script.py`: `::pramana::keep_if_better` and `::pramana::policy_*` count as self-guarding
   mutations, and `::pramana::recipe_*` as mutators. Without this, a script that only called
   `::pramana::policy_all` would take maya's query path: never measured, never banked, and the
   procs' internal `open_checkpoint` reverts would go unseen by the harness.

The `::opt::` Tcl library is still sourced in every variant. The harness probes
`::opt::diagnose` to decide whether the recipes are loaded, and the script validator recommends
the `::opt::try_keep_if_better` guard. In none/pramana only that guard is documented. The other
`::opt::` procs exist in the session, but the model is not told about them.

## Known residual hand knowledge in none/pramana

- The duplicate-script refusal message suggests the order "placement directive → phys_opt →
  route → retiming/restructuring/uncombine".
- The per-step `=> Identical WNS AND identical primitive count ... no-op` line in refreshed
  digests is a measured fact about the step, so it was left in.
- `plan_script.validate_script` still enforces maya's directive/option legality constants. That
  is tool truth for xcvu3p, so it was left in.
