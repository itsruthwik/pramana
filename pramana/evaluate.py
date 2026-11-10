"""Evaluate a compiled mithya bundle on designs (normally held-out ones).

For each design: profile -> features -> classify -> sequence_for(bin, budget) -> run all steps
in one Vivado session (candidate steps keep-if-better) -> measure. Everything except the
measurement comes from the bundle: its policy.json (learned by pramana) and its fixed shim
(classify/recipes/executor + protocol.py), see pramana/mithya_shim/README.md. Compared against the
design's Explore baselines (no-KB reference). Results go to <eval campaign>/results.jsonl.

  python -m pramana.evaluate <train_campaign> <eval_campaign> [budget_s]
The eval campaign must already be initialised and profiled (pramana init/profile/baseline).
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from . import profile_ext
from .campaign import Campaign
from .config import Config

BUNDLE_MODULES = ("protocol", "policy", "extract", "selector", "classify", "recipes",
                  "executor", "features", "measure")


def load_bundle(mdir: Path) -> dict:
    """Import a mithya bundle's modules (they import each other by bare name) without leaving
    them in sys.modules, so several bundles can be loaded in one process."""
    mdir = Path(mdir).resolve()
    if not (mdir / "selector.py").exists():
        raise SystemExit(f"{mdir}: old-format bundle (no selector.py); re-run compile, or "
                         f"python -m pramana.compile <kb.json> {mdir}")
    for n in BUNDLE_MODULES:
        sys.modules.pop(n, None)
    sys.path.insert(0, str(mdir))
    try:
        return {n: importlib.import_module(n) for n in BUNDLE_MODULES}
    finally:
        sys.path.remove(str(mdir))
        for n in BUNDLE_MODULES:
            sys.modules.pop(n, None)


def run(train: Campaign, ev: Campaign, budget_s: float = 3000, log=print,
        bundle: Path | None = None, designs: list[str] | None = None,
        label: str | None = None) -> list[dict]:
    """bundle: a mithya bundle dir (default: the training campaign's current one);
    designs: subset of the eval campaign's designs; label: names the KB version in tags."""
    mdir = Path(bundle) if bundle else train.root / "out" / "mithya"
    label = label or train.cfg.name
    m = load_bundle(mdir)
    meas, cls, rec_mod, ex, proto = (m["measure"], m["classify"], m["recipes"],
                                     m["executor"], m["protocol"])
    # the selector reads the extended profile's reports
    profile_ext.run(ev.cfg, [d for d in ev.designs() if not designs or d["id"] in designs], log=log)
    jobs, plan = [], {}
    for d in ev.designs():
        did = d["id"]
        if designs and did not in designs:
            continue
        edir = profile_ext.ext_dir(ev.cfg, d)
        f = meas.design_features(ev.profile_dir(did), edir)
        b = cls.choose_policy(ev.profile_dir(did), edir, f)
        prims = (ev.profile(did)["input"] or {}).get("prims")
        steps = rec_mod.sequence_for(b, budget_s, prims)
        plan[did] = {"bin": b, "steps": [(s.name, s.mode, round(s.est_secs)) for s in steps]}
        jobs.append(ev.job(did, ev.input_state(did), [], "evaluate",
                           tag=f"mithya:{label}:{b}", extra_tcl=ex.policy_tcl(steps, budget_s),
                           timeout_s=budget_s + proto.GRACE_S))
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
    # python -m pramana.evaluate <train> <eval> [budget_s] [bundle dir, e.g. a KB snapshot's
    #   campaigns/<train>/snapshots/n10/out/mithya]
    a = sys.argv[1:]
    run(Campaign(Config.load(a[0])), Campaign(Config.load(a[1])),
        float(a[2]) if len(a) > 2 else 3000, bundle=Path(a[3]) if len(a) > 3 else None,
        label=f"{a[0]}_{Path(a[3]).parent.parent.name}" if len(a) > 3 else None)
