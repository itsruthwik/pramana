"""Tune the generator's own settings on training folds, without new Vivado runs.

A finished campaign is replayed as if it had been run with less: fewer search rounds, or one
part of the generator switched off (audits, hypothesis tests, discovery). For each variant:
  - the runs it would have made are the campaign's runs that pass the variant's filter;
  - a policy candidate is available only if some design's lineage for it consists of such
    runs (so the variant could have found it);
  - the one-bin policy is picked on all families but one and scored on the held-out family
    (LOFO), against each design's best result in the FULL campaign.
The variant's cost is the Vivado time of its runs. Comparing score vs cost across variants
shows which settings the next campaign should use. Approximation: a variant that ran less
would also have chosen later runs differently; the replay can't see that, so the score of a
cheaper variant is an optimistic estimate of what it would have found.

  python -m pramana.tune <campaign>    -> campaigns/<c>/tune.json, tune.md
"""
from __future__ import annotations

import json
import statistics
import sys

from . import distill
from .campaign import Campaign
from .config import Config
from .confirm import flatten
from .space import chain_key


def _lineage_runs(c: Campaign, did: str, by: dict) -> dict[str, list[list[str]]]:
    """policy chain key -> list of lineages (run ids) on this design that produce it."""
    out: dict[str, list[list[str]]] = {}
    for r in c.results(did):
        if not r["valid"]:
            continue
        ids, units, sid = [], [], r["run_id"]
        while sid != "input" and sid in by:
            x = by[sid]
            ids.insert(0, sid)
            units.insert(0, x["chain"])
            sid = x["start"]
        if sid != "input" or any(by[i].get("extra_tcl") for i in ids):
            continue
        out.setdefault(chain_key(flatten(units)), []).append(ids)
    return out


def variants(c: Campaign) -> dict[str, callable]:
    rounds = sorted({r["round"] for r in c.results() if isinstance(r.get("round"), int)})
    v = {"full": lambda r: True}
    for k in rounds[:-1]:
        v[f"rounds<={k}"] = lambda r, k=k: not isinstance(r.get("round"), int) or r["round"] <= k
    v["no_audit"] = lambda r: r["purpose"] != "audit"
    v["no_test"] = lambda r: r["purpose"] != "test"
    v["no_discover"] = lambda r: r["purpose"] not in ("discover", "recipe_repeat", "recipe_transfer")
    v["exploit_only"] = lambda r: r["purpose"] not in ("audit", "test", "discover",
                                                       "recipe_repeat", "recipe_transfer")
    return v


def run(c: Campaign, log=print) -> list[dict]:
    fam = distill._families(c)
    M, pols = distill.policy_matrix(c)
    designs = [d for d in fam if M and all(d in row for row in M.values())]
    if len(designs) < 3 or len({fam[d] for d in designs}) < 2:
        raise SystemExit("tune: needs >= 3 designs in >= 2 families with every policy replayed")
    oracle = {d: distill._oracle(c, d) for d in designs}
    rs = c.results()
    by = {r["run_id"]: r for r in rs}
    lin = {d: _lineage_runs(c, d, by) for d in designs}
    search_purposes = {"exploit", "test", "audit", "discover", "recipe_repeat", "recipe_transfer"}
    # confirm-stage runs re-run what the search already found; they are evidence for the
    # policy matrix, not a way to find states, so no variant can use them for that
    found_by = search_purposes | {"baseline"}
    out = []
    for name, keep0 in variants(c).items():
        own = lambda r, keep0=keep0: r["purpose"] in found_by and keep0(r)

        def keep(r, own=own):
            # a run counts only if it and every run its start state came from are kept
            while True:
                if not own(r):
                    return False
                if r["start"] == "input":
                    return True
                if r["start"] not in by:
                    return False
                r = by[r["start"]]
        avail = {k for k in M if pols.get(k, {}).get("baseline")
                 or any(all(keep(by[i]) for i in ids)
                        for d in pols.get(k, {}).get("from_designs", []) for ids in lin.get(d, {}).get(k, []))}
        Mv = {k: M[k] for k in avail}
        if not Mv:
            continue
        scores = []
        for F in sorted({fam[d] for d in designs}):
            tr = [d for d in designs if fam[d] != F]
            k = distill._pick(Mv, tr, oracle)
            scores += [distill._frac(Mv[k][d], oracle[d]) for d in designs if fam[d] == F]
        best_found = {d: max([by[i]["result"]["wns"] for i in by if by[i]["design"] == d
                              and by[i]["valid"] and keep(by[i])] + [c.input_state(d)["wns"]])
                      - c.input_state(d)["wns"] for d in designs}
        runs = [r for r in rs if r["purpose"] in search_purposes and keep(r)]
        out.append({"variant": name, "policies_available": len(Mv),
                    "lofo_frac_of_oracle": round(statistics.mean(scores), 3),
                    "found_frac_of_oracle": round(statistics.mean(
                        distill._frac(best_found[d], oracle[d]) for d in designs), 3),
                    "search_runs": len(runs),
                    "search_vivado_h": round(sum(r.get("wall_s") or 0 for r in runs) / 3600, 1)})
        log(f"[tune] {name}: LOFO {out[-1]['lofo_frac_of_oracle']}, found "
            f"{out[-1]['found_frac_of_oracle']}, {out[-1]['search_runs']} search runs "
            f"({out[-1]['search_vivado_h']} Vivado h)")
    (c.root / "tune.json").write_text(json.dumps(out, indent=1))
    L = ["# Generator settings replayed on training folds", "",
         "LOFO = one-bin policy picked on other families, scored on the held-out family "
         "(fraction of each design's best result in the full campaign). found = best result "
         "the variant's own runs reached. Cheaper variants are optimistic (see pramana/tune.py).", "",
         "| variant | policies available | LOFO | found | search runs | Vivado h |", "|--|--|--|--|--|--|"]
    L += [f"| {x['variant']} | {x['policies_available']} | {x['lofo_frac_of_oracle']} | "
          f"{x['found_frac_of_oracle']} | {x['search_runs']} | {x['search_vivado_h']} |" for x in out]
    (c.root / "tune.md").write_text("\n".join(L) + "\n")
    return out


if __name__ == "__main__":
    run(Campaign(Config.load(sys.argv[1])))
