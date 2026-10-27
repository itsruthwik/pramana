"""Evaluate a compiled mithya bundle on designs (normally held-out ones).

For each design: profile -> features -> classify -> sequence_for(bin, budget) -> run all steps
in one Vivado session (candidate steps keep-if-better) -> measure. Compared against the
design's Explore baselines (no-KB reference). Results go to <eval campaign>/results.jsonl.

  python -m pramana.evaluate <train_campaign> <eval_campaign> [budget_s]
The eval campaign must already be initialised and profiled (pramana init/profile/baseline).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from .campaign import Campaign
from .compile import KIB_TCL
from .config import Config


def _load(mod_path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, mod_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def policy_tcl(steps, budget_s: float | None = None) -> str:
    """No step starts after the budget is spent (a running step finishes: Vivado commands
    can't be interrupted cleanly)."""
    lines = [KIB_TCL]
    if budget_s:
        lines.append(f"set ::pramana::deadline [expr {{[clock seconds] + {int(budget_s)}}}]")
    for s in steps:
        body = "; ".join(s.tcl)
        if s.mode == "committed":
            lines.append(f"if {{[::pramana::time_left]}} {{\nputs \"PRAMANA step {s.name} "
                         f"(committed)\"\n{body}\n}} else {{puts \"PRAMANA {s.name} skipped "
                         f"(time budget spent)\"}}")
        else:
            lines.append(f"::pramana::keep_if_better {{{s.name}}} {{{body}}} [pwd]")
    return "\n".join(lines)


def run(train: Campaign, ev: Campaign, budget_s: float = 3000, log=print,
        bundle: Path | None = None, designs: list[str] | None = None,
        label: str | None = None) -> list[dict]:
    """bundle: a mithya bundle dir (default: the training campaign's current one);
    designs: subset of the eval campaign's designs; label: names the KB version in tags."""
    mdir = Path(bundle) if bundle else train.root / "out" / "mithya"
    label = label or train.cfg.name
    feats_mod = _load(mdir / "features.py", f"pramana_eval_features_{label}")
    cls = _load(mdir / "classify.py", f"pramana_eval_classify_{label}")
    rec_mod = _load(mdir / "recipes.py", f"pramana_eval_recipes_{label}")
    jobs, plan = [], {}
    for d in ev.designs():
        did = d["id"]
        if designs and did not in designs:
            continue
        f = feats_mod.from_profile(ev.profile_dir(did))
        b = cls.classify(f)
        prims = (ev.profile(did)["input"] or {}).get("prims")
        steps = rec_mod.sequence_for(b, budget_s, prims)
        plan[did] = {"bin": b, "steps": [(s.name, s.mode, round(s.est_secs)) for s in steps]}
        jobs.append(ev.job(did, ev.input_state(did), [], "evaluate",
                           tag=f"mithya:{label}:{b}", extra_tcl=policy_tcl(steps, budget_s)))
    (ev.root / f"eval_plan_{label}.json").write_text(json.dumps(plan, indent=1))
    ev.runner.run_many(jobs, log=log)
    # identical policies (same Tcl) are the same run: look every job up, new or reused
    from .runner import run_id
    by_id = {r["run_id"]: r for r in ev.results()}
    recs = [by_id[k] for k in (run_id(j["design"], j["start"], j["chain"], 0, j["extra_tcl"])
                                for j in jobs) if k in by_id]
    out = []
    for r in recs:
        base = {x["tag"]: x["d_wns"] for x in ev.results(r["design"]) if x["purpose"] == "baseline"}
        row = {"design": r["design"], "bin": plan[r["design"]]["bin"], "valid": r["valid"],
               "d_wns": r["d_wns"], "secs": r["opt_secs"], "baselines": base}
        out.append(row)
        log(f"[evaluate] {r['design']}: bin={row['bin']} valid={r['valid']} dWNS={r['d_wns']} "
            f"in {r['opt_secs']}s; Explore baselines {base}")
    (ev.root / f"eval_{label}.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    run(Campaign(Config.load(a[0])), Campaign(Config.load(a[1])),
        float(a[2]) if len(a) > 2 else 3000)
