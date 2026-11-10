"""sequence_for(bin, budget_s, prims) -> [Step]: the bin's learned steps, then the learned
extra actions, while the learned cost model says they fit the budget.

Each Step's Tcl assumes an open ROUTED design and leaves it routed.
mode 'committed' = run plainly; 'candidate' = keep-if-better (revert if WNS gets worse).
"""
from dataclasses import dataclass, field

from policy import POLICY
from protocol import DEFAULT_STEP_S, OUT_OF_RANGE_BIN


@dataclass
class Step:
    name: str
    tcl: list
    mode: str
    cmds: list = field(default_factory=list)
    est_secs: float = 0.0


def est_secs(cmds, prims):
    t = 0.0
    for c in cmds:
        m = POLICY["cost_model"].get(c)
        t += m["a"] * prims ** m["b"] if m and prims else DEFAULT_STEP_S
    return t


def sequence_for(bin_name, budget_s, prims=None):
    """bin_name: a policy name from choose_policy (OUT_OF_RANGE_BIN: the default policy, every
    step a candidate)."""
    pols, by_budget = POLICY["policies"], POLICY["by_budget"]
    key = bin_name if bin_name in pols else POLICY["default_policy"]
    base = list(pols.get(key, []))
    if key == POLICY["default_policy"] and POLICY["selector"]["name"] == "one_bin" and by_budget \
            and prims and "ref" in POLICY["cost_model"]:
        rel = budget_s / est_secs(["ref"], prims)
        fit = [b for b in by_budget if b["rel_budget"] <= rel] or by_budget[:1]
        base = fit[-1]["steps"] or base
    names = {s["name"] for s in base}
    steps, used = [], 0.0
    for s in base + [e for e in POLICY["extra_actions"] if e["name"] not in names]:
        mode = "candidate" if bin_name == OUT_OF_RANGE_BIN else s["mode"]
        st = Step(s["name"], s["tcl"], mode, s["cmds"])
        st.est_secs = est_secs(st.cmds, prims)
        if used + st.est_secs > budget_s and steps:
            break
        used += st.est_secs
        steps.append(st)
    return steps
