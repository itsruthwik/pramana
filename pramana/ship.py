"""Shipping decisions, made in S7 learn (distill) and stored in kb["ship"]: which steps each
bin runs (with committed/candidate mode), which extra actions follow, the budget-dependent
policies and the bin for out-of-range designs. The emit step (compile) only formats them.
"""
from __future__ import annotations

from . import flow, funccheck
from .space import chain_key, step_factors


def _ship_by_factor(kb: dict) -> dict[str, str]:
    return {a["factor"]: a["ship_mode"] for a in kb["actions"]}


def step_mode(step: dict, ship: dict) -> str:
    """committed (plain) only if every factor of the step ships plain."""
    fs = step_factors(step)
    return "committed" if fs and all(ship.get(f) == "plain" for f in fs) else "candidate"


def policy_steps(units: list[list[dict]], kb: dict) -> list[dict]:
    """One executable step per unit (a measured run: starts ROUTED, ends ROUTED), so each can
    be kept or reverted exactly as it was measured."""
    ship = _ship_by_factor(kb)
    usable = {f"{r['from_design']}:{r.get('trial')}" for r in kb.get("recipes") or [] if r.get("usable")}
    out = []
    for u in units:
        if not u:   # campaigns before the tcl step type recorded recipe units as []
            continue
        # a custom-Tcl unit that edits the netlist ships only if it passed the functional check
        if any(st["cmd"] == "tcl" and funccheck.edits_netlist(st["tcl"]) and st.get("name") not in usable
               for st in u):
            continue
        cmds, kinds = [], []
        for name, cs, auto in flow.expand(u):
            cmds += cs
            kinds.append(name.split("_", 1)[-1].replace("auto_", ""))
        modes = {step_mode(st, ship) for st in u}
        out.append({"name": chain_key(u), "units": u, "tcl": cmds,
                    "mode": "committed" if modes == {"committed"} else "candidate",
                    "cmds": kinds})
    return out


def bins(kb: dict) -> dict:
    if "ship" in kb:
        return kb["ship"]["bins"]
    return _bins(kb)


def _bins(kb: dict) -> dict:
    pol = kb["policies"]
    if not pol.get("one_bin"):
        return {"type": "none"}
    if pol.get("bins") == "two":
        tb = pol["two_bin"]
        return {"type": "two", "feature": tb["feature"], "threshold": tb["threshold"],
                "low": tb.get("units_low") or [tb["chain_low"]],
                "high": tb.get("units_high") or [tb["chain_high"]],
                "lofo": tb["lofo_frac_of_oracle"], "perm_p": tb["perm_p"]}
    units = pol["one_bin"].get("units") or [pol["one_bin"]["chain"]]
    port = (pol.get("portfolio") or {}).get("units")
    if port:
        # placements first (keep the best of the portfolio), then the policy's post-placement
        # steps; the policy's own placement unit is dropped, the portfolio replaces it
        post = [u for u in units if u and not any(st["cmd"] in ("place", "opt") for st in u)]
        return {"type": "one", "all": port + post, "portfolio": len(port),
                "lofo": pol["one_bin"]["lofo_frac_of_oracle"]}
    return {"type": "one", "all": units, "lofo": pol["one_bin"]["lofo_frac_of_oracle"]}


def extra_actions(kb: dict, n: int = 6) -> list[dict]:
    """Single actions worth trying after the policy, each a step.

    They run on the policy's end state, not on the input, so where the KB has interaction
    evidence for the action after a re-placed state ('placed' kind) that is what ranks it:
    actions that usually hurt there (interfering) or add nothing (substitute) are dropped, and
    the gain after is used instead of the gain from the input. Actions without such evidence
    are ranked by their gain from the input, behind the ones with evidence."""
    if "ship" in kb:
        return kb["ship"]["extra_units"][:n]
    after = {x["step"]: x for x in kb.get("interactions") or [] if x["after_kind"] == "placed"}
    out = []
    # placements already in the portfolio are not repeated; other placer directives stay as
    # extras (cheap insurance: on eval1 a late wirelength-driven re-place gave the largest gains)
    port = (kb["policies"].get("portfolio") or {}).get("units") or []
    in_port = {(st["cmd"], st.get("directive")) for u in port for st in u}
    for a in kb["actions"]:
        p = a["profile"]
        if p["p_gain"] < 0.5 or p["median_d_wns"] <= 0 or a["factor"].count(":") != 1:
            continue
        cmd, rest = a["factor"].split(":", 1)
        if (cmd, rest.split("=", 1)[-1]) in in_port:
            continue
        if rest.startswith("directive="):
            st = {"cmd": cmd, "directive": rest.split("=", 1)[1]}
            if cmd == "route":
                st["opts"] = {"+ripup": True}
        else:
            continue
        cost = max(p["rel_cost"] or 1, 0.1)
        x = after.get(f"{cmd}:{st['directive']}")
        if x:
            if x["type"] in ("interfering", "substitute") or x["median_after"] <= 0:
                continue
            out.append(((1, x["median_after"] / cost), st))
        else:
            out.append(((0, p["median_d_wns"] / cost), st))
    out.sort(key=lambda x: (-x[0][0], -x[0][1]))
    return [st for _, st in out[:n]]


def _strip(ss):
    return [{k: s[k] for k in ("name", "tcl", "mode", "cmds")} for s in ss]


def _units(p: dict) -> list:
    return p.get("units") or [p["chain"]]


def decide(kb: dict) -> dict:
    """All shipping decisions for a KB (kb["ship"] if distill already made them): the
    candidate policies as steps, the default policy, the strategy selector, extra actions and
    budget policies. A KB without a learned selector (older KBs) gets the built-in one: ONE_BIN
    for one bin, STUMP's model for its two-bin rule."""
    if "ship" in kb:
        return kb["ship"]
    from .strategy import ONE_BIN, STUMP
    b = _bins(kb)
    sel = kb.get("selector")
    if sel:
        pols = {k: policy_steps(_units(p), kb) for k, p in sel["policies"].items()}
        default = sel["default"]
        if b["type"] == "one" and b.get("portfolio") and \
                sel["policies"][default]["key"] == (kb["policies"].get("one_bin") or {}).get("policy"):
            pols[default] = policy_steps(b["all"] or [], kb)    # with the placement portfolio
        selector = {"name": sel["name"], "file": sel["file"], "model": sel["model"],
                    "code": sel["code"], "lofo": sel["lofo"], "val": sel["val"]}
    elif b["type"] == "two":
        pols = {"low": policy_steps(b["low"] or [], kb), "high": policy_steps(b["high"] or [], kb)}
        vals = [f.get(b["feature"]) for f in (kb.get("design_features") or {}).values()]
        n_low = sum(v is not None and v <= b["threshold"] for v in vals)
        default = "low" if n_low * 2 >= len([v for v in vals if v is not None]) else "high"
        selector = {"name": "stump", "file": "selectors/stump.py", "code": STUMP,
                    "model": {"feature": b["feature"], "threshold": b["threshold"], "low": "low",
                              "high": "high", "fallback": default,
                              "range": (kb.get("feature_ranges") or {}).get(b["feature"])}}
    else:
        pols = {"all": policy_steps(b.get("all") or [], kb)}
        default = "all"
        selector = {"name": "one_bin", "file": "selectors/one_bin.py", "code": ONE_BIN,
                    "model": {"policy": "all"}}
    pols = {k: _strip(v) for k, v in pols.items()}
    extra_units = extra_actions(kb)
    policy_keys = {s["name"] for v in pols.values() for s in v}
    extra = [s for s in _strip(policy_steps([[st] for st in extra_units], kb))
             if s["name"] not in policy_keys]
    by_budget = []
    if selector["name"] == "one_bin":
        for x in ((kb["policies"].get("by_budget") or {}).get("budgets") or []):
            if x.get("units"):
                by_budget.append({"rel_budget": x["rel_budget"], "lofo": x["lofo_frac_of_oracle"],
                                  "steps": _strip(policy_steps(x["units"], kb))})
    return {"bins": b, "policies": pols, "default_policy": default, "selector": selector,
            "extra_units": extra_units, "extra_actions": extra, "by_budget": by_budget}
