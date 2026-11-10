"""Adapter to mithya's Rung-shaped engine.

    from ladder import ladder_for
    ladder = [Rung(**r) for r in ladder_for(features_dict, budget_s, prims)]

kind 'committed' = run plainly; 'candidate' = keep only if WNS improves.
cost 'open' = includes a re-place or opt, 'iter' = phys_opt/route class.
"""
from classify import classify
from recipes import sequence_for


def ladder_for(features, budget_s, prims=None):
    out = []
    for s in sequence_for(classify(features), budget_s, prims):
        heavy = any(c in ("place", "opt") for c in s.cmds)
        out.append({"name": s.name[:80], "kind": s.mode, "tcl": "\n".join(s.tcl),
                    "cost": "open" if heavy else "iter", "fresh_from_golden": False,
                    "max_rounds": 0})
    return out
