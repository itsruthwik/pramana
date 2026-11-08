"""S6: confirm wins and measure transfer.

  replay   : each design's best lineage, flattened into one chain, re-run from the input DCP
             (does the stacked result reproduce from scratch?)
  transfer : each distinct best chain run on every other design from its input DCP
             -> a policy x design matrix: the raw material for bins and policies (S7)
  recipes  : each discovered recipe marked general, run on every other design's best state
"""
from __future__ import annotations

import json
import time

from . import flow
from .campaign import Campaign
from .space import chain_key


def lineage_units(c: Campaign, did: str, sid: str) -> list[list[dict]]:
    """The runs from the input DCP to state sid; each is a unit that starts and ends routed."""
    units: list[list[dict]] = []
    while sid != "input":
        r = next(r for r in c.results(did) if r["run_id"] == sid)
        u = list(r["chain"])
        if r.get("extra_tcl"):   # the runner runs the custom Tcl after the chain
            tag = r.get("tag") or "recipe"
            u.append(flow.tcl_step(tag if ":" in tag else f"{did}:{tag}", r["extra_tcl"]))
        units.insert(0, u)
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


# S6's own runs never define a design's best chain: otherwise a transfer or recipe run that
# improves a design changes the policy set, and every resume of S6 transfers new chains.
CONFIRM_PURPOSES = frozenset({"replay", "transfer", "recipe_repeat", "recipe_transfer"})


def policies(c: Campaign, before: str | None = None) -> dict[str, dict]:
    """Distinct best chains across designs: key -> {chain, from_designs}."""
    out: dict[str, dict] = {}
    for d in c.designs():
        best = c.best_state(d["id"], skip_purposes=CONFIRM_PURPOSES, before=before)
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


def select_transfer(c: Campaign, pols: dict[str, dict], k: int) -> set[str]:
    """The best-chain policies worth transferring: all if k <= 0, else k of them. Score = the
    source design's own gain relative to its input slack (|input WNS|, at least 0.1 ns), so
    small and large designs compare; one chain per family first, then by score."""
    best = [x for x in pols if not pols[x].get("baseline")]
    if k <= 0 or len(best) <= k:
        return set(best)
    fam = {d["id"]: d["family"] for d in c.designs()}

    def score(key: str) -> float:
        out = []
        for did in pols[key]["from_designs"]:
            inp = c.input_wns(did)
            out.append((c.best_state(did)["wns"] - inp) / max(abs(inp), 0.1))
        return max(out or [0.0])
    ranked = sorted(best, key=score, reverse=True)
    chosen, seen = [], set()
    for key in ranked:                       # one per family first
        f = {fam.get(d) for d in pols[key]["from_designs"]}
        if not (f & seen):
            chosen.append(key)
            seen |= f
        if len(chosen) == k:
            break
    for key in ranked:                       # then the best remaining
        if len(chosen) == k:
            break
        if key not in chosen:
            chosen.append(key)
    return set(chosen)


def run(c: Campaign, log=print) -> None:
    frozen = c.root / "policies.frozen.json"
    state_f = c.root / "confirm_state.json"
    # where recipe transfers start (each design's best state): fixed when S6 first starts, so
    # a resumed S6 does not start them from states that S6 itself improved
    if state_f.exists():
        cst = json.loads(state_f.read_text())
    else:
        cst = {"as_of": time.strftime("%Y-%m-%d %H:%M:%S"), "skip_confirm": True}
        state_f.write_text(json.dumps(cst, indent=1))
    skip = CONFIRM_PURPOSES if cst.get("skip_confirm", True) else frozenset()

    def start_of(did: str) -> dict:
        return c.best_state(did, skip_purposes=skip, before=cst["as_of"])
    if frozen.exists():
        # a resumed S6 runs the same policy set as the run it continues
        pols = json.loads(frozen.read_text())
        designs = {d["id"] for d in c.designs()}
        for p in pols.values():
            p["from_designs"] = [x for x in p["from_designs"] if x in designs]
        pols = {k: p for k, p in pols.items() if p["from_designs"] or p.get("baseline")}
        log(f"[confirm] using frozen policy set {frozen.name}")
    else:
        pols = policies(c)
        keep = select_transfer(c, pols, getattr(c.cfg, "confirm_top_k", 0))
        for key in pols:
            pols[key]["transferred"] = bool(pols[key].get("baseline")) or key in keep
        frozen.write_text(json.dumps(pols, indent=1))
    (c.root / "policies.json").write_text(json.dumps(pols, indent=1))
    n_best = sum(1 for p in pols.values() if not p.get("baseline"))
    n_tr = sum(1 for p in pols.values() if not p.get("baseline") and p.get("transferred"))
    log(f"[confirm] transferring {n_tr} of {n_best} best chains "
        f"(confirm_top_k={getattr(c.cfg, 'confirm_top_k', 0)}) + {len(pols) - n_best} references")
    jobs = []
    for d in c.designs():
        inp = c.input_state(d["id"])
        for k, p in pols.items():
            purpose = "replay" if d["id"] in p["from_designs"] else "transfer"
            if purpose == "transfer" and not p["transferred"]:
                continue
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
                    jobs.append(c.job(o["id"], start_of(o["id"]), [], "recipe_transfer",
                                      tag=f"{d['id']}:{cd['name']}", extra_tcl=tcl))
    # a run that already hit the run timeout is not repeated: its result is "does not finish"
    from .runner import run_id
    timed_out = {r["run_id"] for r in c.runner.load() if r["status"] == "timeout"}
    n0 = len(jobs)
    jobs = [j for j in jobs if run_id(j["design"], j["start"], j["chain"], j.get("rep", 0),
                                      j.get("extra_tcl", "")) not in timed_out]
    if n0 - len(jobs):
        log(f"[confirm] {n0 - len(jobs)} runs timed out before; not repeated")
    jobs.sort(key=lambda j: j["dcp_mb"])
    c.runner.run_many(jobs, log=log)
    log(f"[confirm] {len(pols)} policies x {len(c.designs())} designs; recipes transferred")
    functional_checks(c, log)


def functional_checks(c: Campaign, log=print) -> None:
    """Recipes that edit the netlist directly (not through Vivado's own optimisation
    commands) and gained on their own design: simulate the start state against the result
    (funccheck: sampled random vectors, not proof). Results go to
    discover/<design>/funccheck_<trial>.json; distill reads them."""
    from concurrent.futures import ThreadPoolExecutor
    from . import funccheck
    todo = []
    for d in c.designs():
        did = d["id"]
        cand = c.root / "discover" / did / "candidates.json"
        if not cand.exists():
            continue
        try:
            cands = json.loads(cand.read_text())
        except json.JSONDecodeError:
            continue
        own = {r.get("tag"): r for r in c.results(did) if r["purpose"] == "discover"}
        for cd in cands[:3]:
            tcl_p = c.root / "discover" / did / "trials" / f"{cd.get('trial')}.tcl"
            r0 = own.get(cd.get("trial"))
            out_p = c.root / "discover" / did / f"funccheck_{cd.get('trial')}.json"
            if out_p.exists() or not tcl_p.exists() or not r0 or not r0["valid"] \
                    or not r0.get("dcp") or (r0["d_wns"] or 0) <= c.noise_floor(did):
                continue
            if not funccheck.edits_netlist(tcl_p.read_text()):
                continue
            start = c.state(did, r0["start"])
            todo.append((did, cd["trial"], start["dcp"], r0["dcp"], out_p))

    def one(t):
        did, trial, a, b, out_p = t
        try:
            res = funccheck.check(a, b, out_p.parent / f"funccheck_{trial}")
        except Exception as e:   # a failed check leaves the recipe unverified
            res = {"status": "error", "error": str(e)[:500]}
        out_p.write_text(json.dumps(res, indent=1))
        log(f"[confirm] functional check {did}:{trial}: {res['status']}")
    with ThreadPoolExecutor(2) as ex:
        list(ex.map(one, todo))
