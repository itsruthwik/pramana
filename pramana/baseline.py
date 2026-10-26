"""S2: Explore per command, plus a repeat for the noise floor.

These runs are the controls for everything later, the evidence the pruner reads (Explore runs
every internal pass and logs what each did), and the first cost/noise measurements.
"""
from __future__ import annotations

from .campaign import Campaign

BASELINES = {
    "route_explore": [{"cmd": "route", "directive": "Explore", "opts": {"+ripup": True}}],
    "place_explore": [{"cmd": "place", "directive": "Explore"}],
    "physopt_explore_postroute": [{"cmd": "phys_opt", "directive": "Explore"}],
    "physopt_explore_preroute": [{"cmd": "place", "directive": "Default"},
                                 {"cmd": "phys_opt", "directive": "Explore"}],
    "opt_explore": [{"cmd": "opt", "directive": "Explore"}],
    # control for opt_explore: opt forces a re-place, so re-place alone must be measured too
    "place_default": [{"cmd": "place", "directive": "Default"}],
}
# Vivado is deterministic for a fixed thread count, so identical repeats show no spread.
# Repeats run with a different thread count instead: same algorithm, different tie-breaking,
# which gives a usable noise floor. One routing repeat, one re-place repeat.
REPEATS = ["route_explore", "physopt_explore_preroute"]
REPEAT_THREADS = 2


def jobs(c: Campaign) -> list[dict]:
    out = []
    for d in c.designs():
        st = c.input_state(d["id"])
        for tag, chain in BASELINES.items():
            out.append(c.job(d["id"], st, chain, "baseline", tag=tag))
        for tag in REPEATS:
            out.append(c.job(d["id"], st, BASELINES[tag], "baseline", tag=tag + "_rep", rep=1,
                             threads=REPEAT_THREADS))
    # cheap designs first so evidence for them arrives early
    out.sort(key=lambda j: j["dcp_mb"])
    return out


def run(c: Campaign, log=print) -> list[dict]:
    return c.runner.run_many(jobs(c), log=log)


def baseline_runs(c: Campaign, did: str) -> dict[str, dict]:
    return {r["tag"]: r for r in c.results(did) if r["purpose"] == "baseline"}
