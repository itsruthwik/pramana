"""S6: confirm wins and measure transfer.

  replay   : each design's best lineage, flattened into one chain, re-run from the input DCP
             (does the stacked result reproduce from scratch?)
  transfer : each distinct best chain run on every other design from its input DCP
             -> a policy x design matrix: the raw material for bins and policies (S7)
  recipes  : each discovered recipe marked general, run on every other design's best state
"""
from __future__ import annotations

import json

from . import flow
from .campaign import Campaign
from .space import chain_key


def lineage_units(c: Campaign, did: str, sid: str) -> list[list[dict]]:
    """The runs from the input DCP to state sid; each is a unit that starts and ends routed."""
    units: list[list[dict]] = []
    while sid != "input":
        r = next(r for r in c.results(did) if r["run_id"] == sid)
        units.insert(0, r["chain"])
        sid = r["start"]
    return units


def flatten(units: list[list[dict]]) -> list[dict]:
    """One chain equivalent to running the units in sequence: each unit's trailing
    auto place/route (added by flow.expand) becomes an explicit step, so the next unit
    starts from a routed design exactly as it did when measured."""
    out: list[dict] = []
    for u in units:
        out += u
        exp = flow.expand(u)
        trailing = []
        for name, _, auto in reversed(exp):
            if not auto or not name.startswith("auto_"):
                break
            trailing.insert(0, {"cmd": name.split("_", 1)[1]})
        out += trailing
    return out


def lineage_chain(c: Campaign, did: str, sid: str) -> list[dict]:
    return flatten(lineage_units(c, did, sid))


def policies(c: Campaign) -> dict[str, dict]:
    """Distinct best chains across designs: key -> {chain, from_designs}."""
    out: dict[str, dict] = {}
    for d in c.designs():
        best = c.best_state(d["id"])
        if best["id"] == "input":
            continue
        units = lineage_units(c, d["id"], best["id"])
        ch = flatten(units)
        k = chain_key(ch)
        out.setdefault(k, {"chain": ch, "units": units, "from_designs": []})["from_designs"].append(d["id"])
    # also the Explore baselines as reference policies
    from .baseline import BASELINES
    for tag, ch in BASELINES.items():
        out.setdefault(chain_key(ch), {"chain": ch, "units": [ch], "from_designs": [], "baseline": tag})
    return out


def run(c: Campaign, log=print) -> None:
    pols = policies(c)
    (c.root / "policies.json").write_text(json.dumps(pols, indent=1))
    jobs = []
    for d in c.designs():
        inp = c.input_state(d["id"])
        for k, p in pols.items():
            purpose = "replay" if d["id"] in p["from_designs"] else "transfer"
            jobs.append(c.job(d["id"], inp, p["chain"], purpose, tag=k[:60]))
    # discovered general recipes -> other designs' best states
    for d in c.designs():
        cand = c.root / "discover" / d["id"] / "candidates.json"
        if not cand.exists():
            continue
        try:
            cands = json.loads(cand.read_text())
        except json.JSONDecodeError:
            continue
        own = {r.get("tag"): r for r in c.results(d["id"]) if r["purpose"] == "discover"}
        for cd in cands[:2]:
            if not cd.get("general"):
                continue
            tcl_p = c.root / "discover" / d["id"] / "trials" / f"{cd['trial']}.tcl"
            r0 = own.get(cd["trial"])
            # only recipes that actually beat their start state are worth transferring
            if not tcl_p.exists() or not r0 or not r0["valid"] or \
                    (r0["d_wns"] or 0) <= c.noise_floor(d["id"]):
                continue
            tcl = tcl_p.read_text()
            for o in c.designs():
                if o["id"] == d["id"]:
                    jobs.append(c.job(o["id"], c.state(o["id"], json.loads(
                        (c.root / "discover" / d["id"] / "meta.json").read_text())["start"]),
                        [], "recipe_repeat", tag=f"{d['id']}:{cd['name']}", extra_tcl=tcl, rep=1))
                else:
                    jobs.append(c.job(o["id"], c.best_state(o["id"]), [], "recipe_transfer",
                                      tag=f"{d['id']}:{cd['name']}", extra_tcl=tcl))
    jobs.sort(key=lambda j: j["dcp_mb"])
    c.runner.run_many(jobs, log=log)
    log(f"[confirm] {len(pols)} policies x {len(c.designs())} designs; recipes transferred")
