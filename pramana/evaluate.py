"""Evaluate a compiled mithya bundle on designs (normally held-out ones).

For each design: profile -> features -> classify -> sequence_for(bin, budget) -> run all steps
in one Vivado session (candidate steps keep-if-better) -> measure. Everything except the
measurement comes from the bundle: its policy.json (learned by pramana) and its fixed shim
(classify/recipes/executor + protocol.py), see pramana/mithya_shim/README.md.

Fair accounting: the profile mithya needs (S1 profile plus the extended reports its selector
reads) is charged to the budget, so the policy runs in what is left. Each Explore baseline
(no-KB reference) is scored the way mithya runs, keep-if-better within the same budget: the
best routed state it reached by budget_s (0 if none), next to its unlimited result. Results go to <eval campaign>/results.jsonl.

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
from .strategy import timeline

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
    pol = json.loads((mdir / "policy.json").read_text())
    jobs, plan = [], {}
    for d in ev.designs():
        did = d["id"]
        if designs and did not in designs:
            continue
        edir = profile_ext.ext_dir(ev.cfg, d)
        f = meas.design_features(ev.profile_dir(did), edir)
        prof = ev.profile(did)
        prims = (prof["input"] or {}).get("prims")
        prof_s = profile_secs(prof, edir, pol.get("ext_reports"))
        left = max(budget_s - prof_s, 60.0)
        b = cls.choose_policy(ev.profile_dir(did), edir, f, rec_mod.rel_budget(left, prims))
        steps = rec_mod.sequence_for(b, left, prims)
        plan[did] = {"bin": b, "profile_s": prof_s, "run_budget_s": round(left),
                     "steps": [(s.name, s.mode, round(s.est_secs)) for s in steps]}
        jobs.append(ev.job(did, ev.input_state(did), [], "evaluate",
                           tag=f"mithya:{label}:{b}", extra_tcl=ex.policy_tcl(steps, left),
                           timeout_s=left + proto.GRACE_S))
    (ev.root / f"eval_plan_{label}.json").write_text(json.dumps(plan, indent=1))
    ev.runner.run_many(jobs, log=log)
    # identical policies (same Tcl) are the same run: look every job up, new or reused
    from .runner import run_id
    by_id = {r["run_id"]: r for r in ev.results()}
    recs = [by_id[k] for k in (run_id(j["design"], j["start"], j["chain"], 0, j["extra_tcl"])
                                for j in jobs) if k in by_id]
    out = []
    for r in recs:
        did = r["design"]
        w0 = ev.input_state(did)["wns"]
        base, base_b = {}, {}
        for x in ev.results(did):
            if x["purpose"] == "baseline":
                base[x["tag"]] = x["d_wns"]
                tl = timeline(x) if x["valid"] else []
                base_b[x["tag"]] = round(max([w - w0 for t, w in tl if t <= budget_s] + [0.0]), 4)
        p = plan[did]
        row = {"design": did, "bin": p["bin"], "valid": r["valid"], "d_wns": r["d_wns"],
               "secs": r["opt_secs"], "profile_s": p["profile_s"],
               "total_s": round((r["opt_secs"] or 0) + p["profile_s"], 1),
               "baselines": base, "baselines_at_budget": base_b,
               "best_baseline_at_budget": max(base_b.values(), default=None)}
        out.append(row)
        log(f"[evaluate] {did}: bin={row['bin']} valid={r['valid']} dWNS={r['d_wns']} in "
            f"{row['total_s']}s (profile {p['profile_s']}s); Explore baselines within "
            f"{budget_s:.0f}s {base_b} (unlimited {base})")
    (ev.root / f"eval_{label}.json").write_text(json.dumps(out, indent=1))
    return out


def profile_secs(prof: dict, edir: Path, ext_reports) -> float:
    """Seconds mithya spends measuring a design: the S1 profile plus the extended reports its
    selector reads (ext_reports None: all; measured per report in training's report_secs.json)."""
    s = prof.get("profile_secs") or 0.0
    rs = Path(edir) / "report_secs.json"
    if rs.exists():
        secs = json.loads(rs.read_text())
        s += sum(v for k, v in secs.items() if ext_reports is None or k in ext_reports)
    return round(s, 1)


if __name__ == "__main__":
    # python -m pramana.evaluate <train> <eval> [budget_s] [bundle dir, e.g. a KB snapshot's
    #   campaigns/<train>/snapshots/n10/out/mithya]
    a = sys.argv[1:]
    run(Campaign(Config.load(a[0])), Campaign(Config.load(a[1])),
        float(a[2]) if len(a) > 2 else 3000, bundle=Path(a[3]) if len(a) > 3 else None,
        label=f"{a[0]}_{Path(a[3]).parent.parent.name}" if len(a) > 3 else None)
