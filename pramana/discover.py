"""S5: per-design discovery of custom Tcl recipes by a Claude Code agent.

The agent starts from the design's best checkpoint after search, with a fixed trial budget.
It writes Tcl recipes (set_property / get_* style, targeted phys_opt, clock over-constraint,
local re-place/re-route) and runs them through `trial.py`. Its trials are normal results; the
best ones go to S6 for repeat and transfer checks. Mostly composing validated actions is
encouraged: free-form procedures are rarely legal (17.9% in LATCH).
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import evidence, llm
from .campaign import Campaign
from .config import REPO

PROMPT = """You are an expert in AMD Vivado post-route timing closure (UltraScale+, Vivado {ver}).
Your job: find custom Tcl recipes that improve WNS on one routed design beyond what a search
over standard directives/options already achieved.

{brief}

## Search so far on this design (dWNS is vs each run's start state)
{results}

## Your starting point
Trials run on state `{start}` (WNS {start_wns} ns), reached from the input DCP by:
  {lineage}
That checkpoint is already OPEN when your recipe runs. Do not call open_checkpoint, exit, or
write files outside your working directory. After your recipe, pramana re-routes if the
design is not fully routed, then measures. A result counts only if fully routed with WHS >= 0.

## How to run trials (budget: {max_trials} trials; each takes minutes)
  ./trial submit recipe_NAME.tcl   # prints trial id, e.g. t01
  ./trial wait t01                 # blocks <= 9 min; repeat if still running
  ./trial list
Stay in your working directory (the current directory); keep all your files there. Use a Bash
timeout of 600000 ms for `wait`. You may submit up to 3 trials before waiting.

Useful tools (all Vivado Tcl): get_timing_paths -max_paths N -nworst 1, get_cells/get_nets
-of_objects, get_property, set_property (e.g. on cells/nets), phys_opt_design with targeted
options (-force_replication_on_nets, -critical_cell_opt, -placement_opt, -critical_pin_opt,
-fanout_opt, -retime, -routing_opt), route_design -nets / -unroute -nets,
place_design -unplace + place_design on cell subsets (unplace_cell / place_cell), temporary
clock over-constraint (set_clock_uncertainty -setup X [get_clocks ...] before an optimisation,
then set it back to 0 before the end), report_* for diagnosis inside the recipe (puts output
appears in the trial log). Prefer general recipes (queries like get_timing_paths), not
hard-coded cell names, so they can transfer to other designs.

## Deliverable
When done (budget used or no more ideas), write `candidates.json` in your working directory:
  [{{"trial": "t03", "name": "short_name", "general": true, "rationale": "why it works"}}, ...]
listing your best trials (best first). Then reply with a one-paragraph summary.
"""


def run(c: Campaign, log=print, max_trials: int | None = None, designs: list[str] | None = None) -> None:
    max_trials = max_trials or getattr(c.cfg, "discover_trials", 6)
    ids = designs or [d["id"] for d in c.designs()]

    def one(did):
        td = c.root / "discover" / did
        if (td / "candidates.json").exists():
            return
        td.mkdir(parents=True, exist_ok=True)
        wrapper = td / "trial"
        wrapper.write_text(f'#!/bin/bash\nexport PYTHONPATH={REPO}${{PYTHONPATH:+:$PYTHONPATH}}\n'
                           f'exec {sys.executable} -m pramana.trial {c.cfg.name} {did} "$@"\n')
        wrapper.chmod(0o755)
        best = c.best_state(did)
        (td / "meta.json").write_text(json.dumps({"start": best["id"], "start_wns": best["wns"],
                                                  "max_trials": max_trials}))
        prompt = PROMPT.format(
            ver=c.cfg.vivado_version, brief=evidence.design_brief(c, did),
            results=evidence.results_table(c, did, limit=60), start=best["id"],
            start_wns=best["wns"], lineage=" > ".join(c.lineage(did, best["id"])) or "(input)",
            max_trials=max_trials)
        log(f"[discover] {did}: agent starting from {best['id']} (WNS {best['wns']})")
        rec = llm.agent(c.cfg, "discover", did, prompt, cwd=td)
        log(f"[discover] {did}: agent done (turns={rec.get('num_turns')}, error={rec.get('is_error')})")
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(one, ids))
    for did in ids:
        trials = [r for r in c.results(did) if r["purpose"] == "discover"]
        best = max((r for r in trials if r["valid"]), key=lambda r: r["d_wns"] or -9, default=None)
        log(f"[discover] {did}: {len(trials)} trials, best dWNS "
            f"{best['d_wns'] if best else None} (valid {sum(r['valid'] for r in trials)})")
