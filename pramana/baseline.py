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
# Vivado is deterministic: identical repeats, and repeats with another thread count (tried in
# dev2: 0.000 ns spread on 20/20 design x chain pairs), give no spread. So no repeats; the
# noise floor is cfg.noise_floor_ns until a perturbation-based estimate exists.
REPEATS: list[str] = []
REPEAT_THREADS = 2


# Placer-directive sweep: every remaining placer directive once from the input. Placement
# choice carried the largest transferable gains on eval1, and a portfolio of placements can
# only be chosen from a design x directive matrix measured on every design. Quick and
# RuntimeOptimized trade quality for speed and are left out.
SWEEP_SKIP = {"Default", "Explore", "Quick", "RuntimeOptimized"}


def place_sweep(spec: dict) -> dict[str, list[dict]]:
    ds = spec["commands"]["place"]["directives"]
    return {f"place_{d}": [{"cmd": "place", "directive": d}] for d in sorted(ds)
            if d not in SWEEP_SKIP}


def reference_chains(c: Campaign) -> dict[str, list[dict]]:
    return {**BASELINES, **place_sweep(c.spec())}


def jobs(c: Campaign) -> list[dict]:
    out = []
    refs = reference_chains(c)
    for d in c.designs():
        st = c.input_state(d["id"])
        for tag, chain in refs.items():
            out.append(c.job(d["id"], st, chain, "baseline", tag=tag))
        for tag in REPEATS:
            out.append(c.job(d["id"], st, BASELINES[tag], "baseline", tag=tag + "_rep", rep=1,
                             threads=REPEAT_THREADS))
    # cheap designs first so evidence for them arrives early
    out.sort(key=lambda j: j["dcp_mb"])
    return out


def run(c: Campaign, log=print) -> list[dict]:
    """Reference runs go to the framework store; ones already there are skipped."""
    return c.store_runner.run_many(jobs(c), log=log)


def baseline_runs(c: Campaign, did: str) -> dict[str, dict]:
    return {r["tag"]: r for r in c.results(did) if r["purpose"] == "baseline"}
