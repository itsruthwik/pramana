# PACT / FDAgents (vendored copy for pramana evaluation)

Source: the public PACT artifact (arXiv:2608.23602v2, team Fu_Do_Fdo, FPL'26 top five), tag
`v2.0.0` = commit `4081062` "PACT FPL'26 Top-5 contest artifact v2.0.0", read from the local
clone `/mnt/vault1/kmhatre/workspace/FPL_26_contest_asu/data/PACT`. `consumers/pact/` is
git-ignored; `setup.sh` recreates it: `git archive` of the code paths (FDAgents, VivadoMCP,
RapidWrightMCP, knowledge, validate_dcps.py, Makefile, pyproject, licences), a symlink to the
built RapidWright of the contest checkout (`contest_winners/4_Fu_Do_Fdo/RapidWright`), then
`pact_pramana.patch`. The Python is that checkout's `.venv` (Python 3.13, see
`pramana/pact_eval.py` `PACT_PYTHON`). Note: `contest_winners/status.md` records that about 20
scripts the v2.0.0 code calls are missing from the sanitized release; those steps are skipped.

## How PACT consumes knowledge (read from the code)

- `FDAgents/knowledge_base.py`: knowledge is a YAML list of *cases* (`Case`: id, description,
  signature, confidence, bottleneck, suggested_skill_order, initial_params, core_skills,
  budget_hint_s, evidence, notes). Signatures are soft ranges over `DesignFeatures` fields;
  `match()` ranks cases by overlap. Files: `knowledge.cases_path` from config plus
  `FDAGENTS_KNOWLEDGE_PATHS` (colon-separated). Raw Tcl is rejected (keys command/tcl/script/
  tcl_ref), so pramana's recipes cannot be sourced; they map to PACT's typed presets.
- Shipped defaults (`config.yaml`): `cases_path: knowledge/no_replay_empty_cases.yaml`,
  `skills_guide_path: knowledge/no_replay_empty_skills_guide.yaml`, `min_match_score: 1.0`,
  `max_cases_in_prompt: 0`, `react.enabled: true`. The contest entry (`make run-submission`)
  passes no config, so a scored run used an empty corpus. The hand-written library
  (`knowledge/cases.yaml`, 18 cases; `knowledge/skills_guide.yaml`) is present but off.
- In staged-react mode (the default) `agent.py` sets `matches = []`, `target_presets = {}` and
  `rendered_matches = []` outright, and `prompts.build_analyze_user` / `build_decide_user`
  delete `case_matches`. The Decision LLM ranks a code-built action menu from
  `memory.state_for_staged_llm()` (`stages.rank_public_action_menu` passes that state whole).
  So in v2.0.0 no case text reaches the LLM in any configuration.

## Changes to the copy (all marked `[pramana]`, see `pact_pramana.patch`)

1. `FDAgents/memory.py`: new dataclass field `learned_prior: dict` (serialised with the rest
   of memory.json); `state_for_staged_llm()` adds `state["learned_prior"]` when set. The
   Report stage's `_safe_state(report_only=True)` allowlist still drops it; the Decision stage
   sees it.
2. `FDAgents/agent.py`: `pramana_learned_prior(kb, features, cfg)` returns
   `{source, authority, context_md, matched_cases}` when `FDAGENTS_PRAMANA_CONTEXT` names a
   pramana `context.md`: the text plus `kb.render_matches(kb.match(features))` (computed even in
   staged mode). Assigned to `memory.learned_prior` at the three places the agent computes
   `rendered_matches`. Without the env var the patch is inert.

3. Harness robustness for non-OpenAI models (both off by default, so the copy behaves as
   shipped unless `pramana/pact_eval.py` turns them on in its config override):
   - `FDAgents/stages.py`: the two terminal selectors (`select_action_menu_entry`,
     `select_replay_action`) read `react.selector_turns` (shipped behaviour = 1 turn). With
     deepseek-v4-pro over OpenRouter the forced `tool_choice` was answered in prose on the
     second round, and one miss ended the whole run (`llm_decision_unavailable`).
   - `FDAgents/agent.py`: with `react.retry_invalid_decisions: true`, an invalid decision is
     retried like a provider failure (bounded by `react.provider_failure_retries`, shipped 3)
     instead of stopping the run.

Nothing else changes: PACT's skills, action menu, proofs, equivalence export and budget logic
run as shipped. The prior is advisory text in the state the LLM already reads.

PACT's `clock_tighten` skill re-creates the contest clock at a tighter period for placement and
can leave the winning checkpoint at a *different* period (vexriscv: 1.986 ns instead of 1.57 ns),
reporting WNS against it. `pramana.pact_eval` therefore re-measures the output with the input's
clocks re-imposed; PACT's own `current_best.wns` is recorded as `pact_best_wns` only.

Two more facts about running PACT that cost a run each to learn: it refuses every LLM stage
(`cannot enforce a cost cap without model pricing`) unless the model has an entry in
`llm.pricing`, which `pact_eval` now fetches from OpenRouter and writes into the override; and
it uses the OpenAI Responses API with `reasoning.summary`, `include=reasoning.encrypted_content`
and `store=False`, which OpenRouter serves for deepseek-v4-pro (checked) but not necessarily
for every model.

## Variants in `pramana/pact_eval.py`

| `--kb` | cases | skills guide | match | learned_prior |
|---|---|---|---|---|
| none | empty (shipped) | empty (shipped) | off (shipped 1.0 / 0) | no |
| hand | `knowledge/cases.yaml` | `knowledge/skills_guide.yaml` | 0.35 / 5 | no |
| pramana | `<bundle>/cases.yaml` (pramana.adapters) | empty | 0.35 / 5 | `<bundle>/context.md` |

In `hand`, matched cases are computed by PACT but, as in the release, never shown to the LLM
(only `--no-llm` fallback plans read them). It is the shipped behaviour with its own library
switched on, kept for completeness.
