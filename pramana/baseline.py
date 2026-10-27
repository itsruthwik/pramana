"""S2: Explore per command, plus a repeat for the noise floor.

These runs are the controls for everything later, the evidence the pruner reads (Explore runs
every internal pass and logs what each did), and the first cost/noise measurements.
"""
from __future__ import annotations

from . import flow
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


# Noise estimate: re-run two reference chains with the clocks tightened by a few ps while the
# placer and phys_opt run, restored before the final route and the measurement. The result
# moves only because the tool's trajectory changed, so the spread is the sensitivity that a
# measured "gain" has to exceed. Designs that carry their own clock uncertainty are skipped
# (the step errors out, so the run is invalid and ignored).
NOISE_OF = ["physopt_explore_preroute", "place_explore"]
NOISE_PS = [2, 4]


def perturb_steps(ps: int) -> tuple[dict, dict]:
    on = ("set _p [get_timing_paths -quiet -setup -max_paths 1]; "
          "set _w0 [expr {$_p eq \"\" ? 0 : [get_property SLACK $_p]}]; "
          "set_clock_uncertainty -setup 0 [get_clocks]; "
          "set _p [get_timing_paths -quiet -setup -max_paths 1]; "
          "set _w1 [expr {$_p eq \"\" ? 0 : [get_property SLACK $_p]}]; "
          "if {abs($_w0 - $_w1) > 1e-6} {error \"design sets its own clock uncertainty\"}; "
          f"set_clock_uncertainty -setup {ps / 1000:.3f} [get_clocks]")
    off = "set_clock_uncertainty -setup 0 [get_clocks]"
    return ({"cmd": "tcl", "name": f"perturb_on_{ps}ps", "tcl": on},
            {"cmd": "tcl", "name": "perturb_off", "tcl": off + "\n" + flow.ROUTE_IF_NEEDED})


def noise_chains() -> dict[str, tuple[str, list[dict]]]:
    out = {}
    for tag in NOISE_OF:
        for ps in NOISE_PS:
            on, off = perturb_steps(ps)
            out[f"noise_{tag}_{ps}ps"] = (tag, [on] + BASELINES[tag] + [off])
    return out


def reference_chains(c: Campaign) -> dict[str, list[dict]]:
    return {**BASELINES, **place_sweep(c.spec())}


def jobs(c: Campaign) -> list[dict]:
    out = []
    refs = reference_chains(c)
    for d in c.designs():
        st = c.input_state(d["id"])
        for tag, chain in refs.items():
            out.append(c.job(d["id"], st, chain, "baseline", tag=tag))
        for tag, (of, chain) in noise_chains().items():
            out.append(c.job(d["id"], st, chain, "noise", tag=tag, noise_of=of))
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
