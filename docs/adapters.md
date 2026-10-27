# Adapters: pramana KB -> LATCH / PACT knowledge formats

`pramana/adapters.py` `write_all(kb, b, out_dir)` writes `out_dir/latch/` and `out_dir/pact/`.
Each folder has a README with the field mapping and what had no equivalent. The research question is
whether a generated KB can replace the agents' hand-written one.

## LATCH (arXiv 2610.16625)

- Repo: https://github.com/aprilaihub/LATCH
- Commit read: `c53a8bf5f24159d527eb4508dc3cdda6144ca2aa` (2026-10-14)
- Licence: LATCH code Apache-2.0. The MCP-SDK-derived parts are MIT, and AMD-origin parts keep
  their Apache-2.0 notices (`LICENSING.md`). The paper, data and DCPs are not covered by the
  code licence.
- Where LATCH loads its KB: `experiments/ablation/runtime/expansion/frontier_runner.py`
  `_build_knowledge_context` -> `experiments/ablation/runtime/knowledge/` (`loader.py` reads JSON,
  `projector.py` keeps `flow_cards`, `schema.py` warns about missing card fields).
  `runtime/analysis/dynamic_loop.py` `_compact_knowledge_context` then puts only `flow_cards`
  into the analysis-LLM payload as `knowledge_context`, which the system prompt labels
  heuristic_prior evidence.
  The default file is `experiments/ablation/runtime/knowledge/data/reference_manual_final.json`
  (9 hand-written cards, schema `flow_cards_c2_deploy.v1`). The S3 condition is the only one
  that loads it (`knowledge_mode=reference_manual`).
- LATCH's "expert recipes" are code, not data: the 9 flows in `allowed_flows` are Python/Tcl in
  `optimizer_pipeline/backends/xilinx/legacy.py`. The only data-driven recipe format is the
  `synthesized_recipe` flow's `RecipeSpec` (`optimizer_pipeline/capabilities/intervention/recipe.py`),
  limited to allowlisted primitives and directives. `latch/recipe_specs.json` uses that format.

### Running LATCH S3 with the pramana KB

The study entry `python -m experiments.ablation` has no `--knowledge-base-path` flag; S3 always
loads the default file. Swap that file in a scratch checkout:

```bash
git clone https://github.com/aprilaihub/LATCH /tmp/latch && cd /tmp/latch
git checkout c53a8bf5f24159d527eb4508dc3cdda6144ca2aa
python3.12 -m venv .venv && .venv/bin/python -m pip install -e '.[test,analysis,tools]'
# plus the provider extra (openai | anthropic | google), Vivado, Java 21, RapidWright (see LATCH README)
cp /mnt/vault0/rsunketa/pramana-dev/campaigns/<name>/out/adapters/latch/reference_manual_pramana.json \
   experiments/ablation/runtime/knowledge/data/reference_manual_final.json
.venv/bin/python -m experiments.ablation --condition S3 --mode real \
   --input /abs/design.dcp --output /abs/new_run_dir --tool-config /abs/tools.json \
   --provider <profile> --model <exact model id> --cost-budget-usd <budget>
```

The control is the same command on an unmodified checkout, which uses the hand-written manual.

The older `runner_cli` takes the path directly (LATCH calls it a compatibility CLI, not the
recommended interface):

```bash
.venv/bin/python -m experiments.ablation.runner_cli /abs/design.dcp --out /abs/new_run_dir \
   --knowledge-mode reference_manual \
   --knowledge-base-path /abs/out/adapters/latch/reference_manual_pramana.json  # plus provider/model flags
```

The recipes in `recipe_specs.json` run through
`optimizer_pipeline.backends.xilinx.run_test_mode(input, output, flow="synthesized_recipe",
optimizer_params={"recipe_spec": <recipe>}, run_dir=...)`. No CLI loads them.

Caveats:
- LATCH targets Vivado 2025.1; the dev2 KB was learned on 2025.2.
- I did not execute LATCH. The format was checked by reading its source, and `tests/test_adapters.py`
  restates its loader contract.
- Only 3 of the 9 LATCH flows have pramana evidence (phys_opt, retime, fanout via related
  directives). Most of pramana's knowledge (place/opt/route directives, the policy order,
  custom Tcl recipes) has no LATCH flow to select. See `latch/README.md`.

## PACT (arXiv 2608.23602v2)

The code is not released. The paper cites https://anonymous.4open.science/r/PACT-552C, which on
2026-10-27 had only a README ("code release coming soon", licence "to be determined"). The `pact/`
output is rebuilt from the paper (Sec. III, Table II, Algorithm 1) and is **unverified against
code**: the field names are this adapter's choice.
