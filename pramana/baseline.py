"""S2: one Explore reference run per Vivado command, from each input DCP.

Input: the DCP itself. Not S1: the input check in inputs.py measures part, md5 and input WNS
first.

These runs are the controls for everything later, the evidence the pruner reads (Explore runs
every internal pass and logs what each did), and the first cost measurements.

Reduced 2026-11-04 from 22 runs to 5 per design. Removed, and kept in baseline_v1.py: the
place_default control, the placer-directive sweep, the two re-place chains and the four noise
runs. Consequences: the noise floor is cfg.noise_floor_ns unless later stages repeat a chain,
and distill's placement portfolio sees only the placements that S2 and S4 actually run.
"""
from __future__ import annotations

from . import inputs
from .campaign import Campaign

BASELINES = {
    "route_explore": [{"cmd": "route", "directive": "Explore", "opts": {"+ripup": True}}],
    "place_explore": [{"cmd": "place", "directive": "Explore"}],
    "physopt_explore_postroute": [{"cmd": "phys_opt", "directive": "Explore"}],
    "physopt_explore_preroute": [{"cmd": "place", "directive": "Default"},
                                 {"cmd": "phys_opt", "directive": "Explore"}],
    "opt_explore": [{"cmd": "opt", "directive": "Explore"}],
}


def reference_chains(c: Campaign | None = None) -> dict[str, list[dict]]:
    return dict(BASELINES)


def jobs(c: Campaign) -> list[dict]:
    out = []
    refs = reference_chains(c)
    for d in c.designs():
        st = c.input_state(d["id"])
        for tag, chain in refs.items():
            out.append(c.job(d["id"], st, chain, "baseline", tag=tag))
    # cheap designs first so evidence for them arrives early
    out.sort(key=lambda j: j["dcp_mb"])
    return out


def run(c: Campaign, log=print) -> list[dict]:
    """Reference runs go to the framework store; ones already there are skipped.

    Self-contained: the input check (inputs.py) gives each design's part, md5 and input WNS
    and excludes unusable designs, so S2 does not need S1 profile to have run."""
    inputs.check_all(c.cfg, c.designs(), log=log)
    return c.store_runner.run_many(jobs(c), log=log)


def baseline_runs(c: Campaign, did: str) -> dict[str, dict]:
    return {r["tag"]: r for r in c.results(did) if r["purpose"] == "baseline"}
