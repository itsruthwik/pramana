"""Diagnose eval1's late re-place gain: placer directive itself, or re-placing a second time?
On corescore_500_mod and rosetta_digit-recognition, from the input DCP:
  wl1     place AltWLDrivenPlacement
  two_wl  place Default > phys_opt Explore > place AltWLDrivenPlacement
  two_def place Default > phys_opt Explore > place Default
"""
from pramana.campaign import Campaign
from pramana.config import Config

c = Campaign(Config.load("eval1"))
P = lambda d: {"cmd": "place", "directive": d}
PO = {"cmd": "phys_opt", "directive": "Explore"}
chains = {"wl1": [P("AltWLDrivenPlacement")],
          "two_wl": [P("Default"), PO, P("AltWLDrivenPlacement")],
          "two_def": [P("Default"), PO, P("Default")]}
jobs = [c.job(did, c.input_state(did), ch, "diag", tag=f"diag_{t}")
        for did in ("corescore_500_mod", "rosetta_digit-recognition") for t, ch in chains.items()]
for r in c.store_runner.run_many(jobs, log=print):
    print("RESULT", r["design"], r["tag"], r["valid"], r["d_wns"], round(r["secs"]))
