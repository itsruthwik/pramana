"""S8: compile kb.json into the maya (LLM) and mithya (deterministic) bundles.

Compilation is deterministic: the same kb.json gives the same files. It fails if a policy or
recipe refers to evidence that doesn't exist.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from . import flow, funccheck, profile as prof_mod, tcl
from .campaign import Campaign
from .config import REPO
from .space import chain_key, step_factors, step_key


def _nobraces(s: str) -> str:
    return s.replace("{", "(").replace("}", ")")


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", s)[:60]


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


# --------------------------------------------------------------------------- maya
KIB_TCL = r"""
namespace eval ::pramana {}
# Run a script; keep its result only if the design stays legal and WNS improves.
proc ::pramana::wns {} {
    set p [get_timing_paths -quiet -setup -max_paths 1 -nworst 1]
    if {$p eq ""} {return -1e9}; return [get_property SLACK $p]
}
proc ::pramana::legal {} {
    set s [report_route_status -return_string]
    set e 1; set u 0
    regexp -line {routing errors[\s.]*:\s*([0-9]+)} $s -> e
    regexp -line {unrouted nets[\s.]*:\s*([0-9]+)} $s -> u
    set h [get_timing_paths -quiet -hold -max_paths 1 -nworst 1]
    set hs 0; if {$h ne ""} {set hs [get_property SLACK $h]}
    return [expr {$e == 0 && $u == 0 && $hs >= 0}]
}
# 1 if a step expected to take `need` seconds still fits before the deadline
proc ::pramana::time_left {{need 0}} {
    if {![info exists ::pramana::deadline]} {return 1}
    return [expr {[clock seconds] + $need < $::pramana::deadline}]
}
proc ::pramana::keep_if_better {tag script {tmpdir /tmp} {need 0}} {
    if {![::pramana::time_left $need]} { puts "PRAMANA $tag skipped (does not fit the time left)"; return 0 }
    set ck [file join $tmpdir "pramana_kib_[pid].dcp"]
    write_checkpoint -force $ck
    set before [::pramana::wns]
    if {[catch {uplevel #0 $script} err]} { puts "PRAMANA $tag error: $err" }
    if {![::pramana::legal]} { catch {route_design} }
    set after [::pramana::wns]
    if {![::pramana::legal] || $after <= $before} {
        puts "PRAMANA $tag reverted (WNS $before -> $after)"
        open_checkpoint $ck
        return 0
    }
    puts "PRAMANA $tag kept (WNS $before -> $after)"
    return 1
}
"""


def recipes_tcl(kb: dict, b: dict) -> str:
    out = ["# Generated by pramana from kb.json (campaign " + kb["meta"]["campaign"] + ").",
           "# Each policy proc assumes an open ROUTED design and leaves it routed.", KIB_TCL]
    chains = {}
    if b["type"] == "one":
        chains["all"] = b["all"]
    elif b["type"] == "two":
        chains["low"], chains["high"] = b["low"], b["high"]
    for name, ch in chains.items():
        out.append(f"proc ::pramana::policy_{name} {{{{tmpdir /tmp}}}} {{")
        for s in policy_steps(ch or [], kb):
            body = "; ".join(s["tcl"])
            if s["mode"] == "committed":
                out.append(f"    # {s['name']} (committed: never regressed in training)")
                out.append(f"    {body}")
            else:
                out.append(f"    ::pramana::keep_if_better {{{s['name']}}} {{{body}}} $tmpdir")
        out.append("}")
    for r in kb["recipes"]:
        if r.get("own_valid") and (r.get("own_d_wns") or 0) > 0 and r.get("usable", True):
            pname = _safe_name(f"recipe_{r['from_design']}_{r['name']}")
            out.append(f"# recipe {r['name']} from {r['from_design']} ({r['scope']}): "
                       f"{(r.get('rationale') or '')[:200]}")
            out.append(f"proc ::pramana::{pname} {{}} {{\n{r['tcl']}\n}}")
    return "\n".join(out) + "\n"


def context_md(kb: dict, b: dict) -> str:
    m = kb["meta"]
    L = [f"## Learned timing-closure knowledge (pramana)",
         "",
         f"PRIOR, not fact: learned from {len(m['designs'])} training designs in "
         f"{len(m['families'])} families ({', '.join(m['families'])}), part {m['part']}, Vivado "
         f"{m['vivado']}. It describes what tended to work on OTHER designs. Check the current "
         f"design's own reports before relying on it. Effects are dWNS in ns (positive = better).",
         ""]
    L.append("### Starting strategy")
    if b["type"] == "one":
        if b["lofo"] is None:
            L.append("One strategy for all designs (chosen on fewer than 3 training designs; "
                     "not yet checked on held-out designs):")
        else:
            L.append(f"One strategy for all designs (held-out families reached {b['lofo']:.0%} of "
                     f"each design's best-known gain):")
        if b.get("portfolio"):
            L.append(f"First try these {b['portfolio']} placements, keeping the best "
                     "(each re-places from the netlist): "
                     + " | ".join(chain_key(u) for u in b["all"][:b["portfolio"]]))
            L.append("Then: " + " | then | ".join(chain_key(u) for u in b["all"][b["portfolio"]:]))
        else:
            L.append("    " + " | then | ".join(chain_key(u) for u in b["all"] or []))
        L.append("Tcl: ::pramana::policy_all (in recipes.tcl).")
    elif b["type"] == "two":
        L.append(f"Two strategies split on feature {b['feature']} at {b['threshold']} "
                 f"(held-out families reached {b['lofo']:.0%} of best-known gain):")
        L.append(f"  {b['feature']} <= {b['threshold']}: " + " | then | ".join(chain_key(u) for u in b["low"] or []))
        L.append(f"  {b['feature']} > {b['threshold']}: " + " | then | ".join(chain_key(u) for u in b["high"] or []))
    bb = (kb["policies"].get("by_budget") or {}).get("budgets") or []
    if b["type"] == "one" and bb:
        L += ["", "### Strategy by time budget",
              "Budget is in multiples of this design's route_design -directive Explore time "
              "(estimate it from a first route or from the design size). Best policy found for each "
              "budget, and the fraction of best-known gain it reached on held-out families:"]
        for x in bb:
            L.append(f"- at least {x['rel_budget']}x: {x['policy']} ({x['lofo_frac_of_oracle']:.0%})")
    L += ["", "### Actions measured from the input checkpoint (one step each)",
          "action | designs | median dWNS | P(gain) | P(regress) | rel. cost | grade | ship",
          "--- | --- | --- | --- | --- | --- | --- | ---"]
    for a in kb["actions"][:30]:
        p = a["profile"]
        L.append(f"{a['factor']} | {p['n_designs']} | {p['median_d_wns']} | {p['p_gain']} | "
                 f"{p['p_regress']} | {p['rel_cost']} | {a['grade']} | {a['ship_mode']}")
    L.append("rel. cost = step time / that design's route Explore time. ship=plain means it never "
             "regressed in training; otherwise run it keep-if-better.")
    inter = [x for x in kb.get("interactions") or [] if x["n_designs"] >= 3 and x["type"] != "neutral"]
    if inter:
        L += ["", "### Interactions: what a step does after earlier steps",
              "after = state that was re-placed (placed) or only changed by post-route steps "
              "(postroute); gain after = WNS change on top of that state; vs alone = the same step "
              "from the input checkpoint.",
              "after | step | type | designs | median gain after | median gain alone | grade",
              "--- | --- | --- | --- | --- | --- | ---"]
        for x in inter[:25]:
            L.append(f"{x['after_kind']} | {x['step']} | {x['type']} | {x['n_designs']} | "
                     f"{x['median_after']} | {x['median_alone']} | {x['grade']}")
        L.append("complement = doing both beat either alone; substitute = helps alone, adds "
                 "nothing after; interfering = usually made the earlier result worse; repeat = "
                 "repeats a step already applied.")
    conds = [x for x in kb["conditions"] if x["status"] == "condition"]
    if conds:
        L += ["", "### Conditions (held on held-out families)"]
        for x in conds:
            L.append(f"- {x['factor']}: {x['rule']} (LOFO accuracy {x['lofo_acc']} vs "
                     f"{x['lofo_majority_acc']} baseline, grade {x.get('grade')})")
    if kb["negative"]:
        L += ["", "### Usually not worth running"]
        seen = set()
        for x in kb["negative"]:
            if x["factor"] in seen:
                continue
            seen.add(x["factor"])
            L.append(f"- {x['factor']}: {x['why']}")
    if kb["signals"]:
        L += ["", "### Mid-run signals"]
        for s in kb["signals"]:
            L.append(f"- {s['signal']}: P(gain) {s['p_gain_given_zero']} when it fires vs "
                     f"{s['p_gain_given_nonzero']} otherwise. {s['use']}.")
    rec = [r for r in kb["recipes"] if r.get("own_valid") and (r.get("own_d_wns") or 0) > 0
           and r.get("usable", True)]
    if rec:
        L += ["", "### Custom recipes found by discovery (procs in recipes.tcl)"]
        for r in rec:
            L.append(f"- ::pramana::{_safe_name('recipe_' + r['from_design'] + '_' + r['name'])}: "
                     f"{r['scope']}; own gain {r['own_d_wns']} ns; helped {len(r['helped_designs'])} "
                     f"other designs; functional check: {r.get('functional', 'n/a')}. "
                     f"{(r.get('rationale') or '')[:200]}")
    if kb.get("notes"):
        L += ["", "### Why (mechanism notes)"]
        for k, v in kb["notes"].items():
            L.append(f"- {k}: {v}")
    pr = kb.get("pruning") or {}
    if pr.get("false_prune_rate") is not None:
        L += ["", f"Pruning audit: {pr['audits']} audit runs, false-prune rate {pr['false_prune_rate']}."]
    return _nobraces("\n".join(L)) + "\n"


# --------------------------------------------------------------------------- mithya
CLASSIFY_PY = '''"""Generated by pramana: classify(features) -> bin, with abstention.

Bins come from kb.json; a bin rule is used only if it beat one bin on held-out families.
Abstains ('abstain') when a feature used by the rule is outside the training range.
"""
BIN_RULE = {bin_rule}
TRAIN_RANGES = {ranges}
MARGIN = 0.1   # fraction of the training span allowed outside the range


def in_range(name, value):
    lo, hi = TRAIN_RANGES.get(name, (None, None))
    if lo is None or value is None:
        return value is not None
    span = (hi - lo) or abs(hi) or 1.0
    return lo - MARGIN * span <= value <= hi + MARGIN * span


def classify(features, thresholds=None):
    rule = dict(BIN_RULE)
    if thresholds:
        rule.update(thresholds)
    if rule["type"] == "one":
        return "all"
    f = rule["feature"]
    v = features.get(f)
    if not in_range(f, v):
        return "abstain"
    return "low" if v <= rule["threshold"] else "high"
'''

RECIPES_PY = '''"""Generated by pramana: sequence_for(bin, budget_s) -> [Step].

Each Step's Tcl assumes an open ROUTED design and leaves it routed.
mode 'committed' = run plainly (never regressed in training);
mode 'candidate' = run keep-if-better (save checkpoint, revert if WNS gets worse).
"""
from dataclasses import dataclass, field
import math

COST_MODEL = {cost_model}   # per command: secs ~ a * prims**b
POLICIES = {policies}
EXTRA = {extra}
# one-bin policy per relative budget (budget / estimated route Explore secs), from the KB
BY_BUDGET = {by_budget}


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
        m = COST_MODEL.get(c)
        t += m["a"] * prims ** m["b"] if m and prims else 120.0
    return t


def sequence_for(bin_name, budget_s, prims=None):
    """Policy steps for the bin, then extra single actions, while the estimated time fits.
    'abstain' runs the default policy with every step as a candidate."""
    key = bin_name if bin_name in POLICIES else "all" if "all" in POLICIES else next(iter(POLICIES), None)
    steps = []
    base = list(POLICIES.get(key, []))
    if key == "all" and BY_BUDGET and prims and "ref" in COST_MODEL:
        rel = budget_s / est_secs(["ref"], prims)
        fit = [b for b in BY_BUDGET if b["rel_budget"] <= rel] or BY_BUDGET[:1]
        base = fit[-1]["steps"] or base
    raw = base + [e for e in EXTRA if e["name"] not in [s["name"] for s in base]]
    used = 0.0
    for s in raw:
        st = Step(s["name"], s["tcl"], "candidate" if bin_name == "abstain" else s["mode"], s["cmds"])
        st.est_secs = est_secs(st.cmds, prims)
        if used + st.est_secs > budget_s and steps:
            break
        used += st.est_secs
        steps.append(st)
    return steps
'''


LADDER_PY = '''"""Generated by pramana: adapter to mithya's engine (Rung-shaped ladder).

    from ladder import ladder_for
    rungs = ladder_for(features_dict, budget_s, prims)   # list of mithya.recipes.Rung kwargs
    ladder = [Rung(**r) for r in rungs]

kind 'committed' = run plainly; 'candidate' = engine keeps it only if WNS improves.
cost 'open' = includes a re-place or opt (place+route class), 'iter' = phys/route class.
"""
from classify import classify
from recipes import sequence_for


def ladder_for(features, budget_s, prims=None):
    b = classify(features)
    out = []
    for s in sequence_for(b, budget_s, prims):
        heavy = any(c in ("place", "opt") for c in s.cmds)
        out.append({"name": s.name[:80], "kind": s.mode, "tcl": "\\n".join(s.tcl),
                    "cost": "open" if heavy else "iter", "fresh_from_golden": False,
                    "max_rounds": 0})
    return out
'''


def run(c: Campaign, log=print) -> None:
    kb = json.loads((c.root / "kb" / "kb.json").read_text())
    run_ids = {r["run_id"] for r in c.results()}
    for a in kb["actions"]:
        missing = [e for e in a["evidence"] if e not in run_ids]
        if missing:
            raise SystemExit(f"compile: action {a['factor']} cites missing runs {missing[:3]}")
    b = bins(kb)
    out = c.root / "out"
    maya, mith = out / "maya", out / "mithya"
    for d in (maya, mith):
        d.mkdir(parents=True, exist_ok=True)
    (maya / "context.md").write_text(context_md(kb, b))
    (maya / "recipes.tcl").write_text(recipes_tcl(kb, b))
    (maya / "knowledge.json").write_text(json.dumps(kb, indent=1, sort_keys=True))
    # mithya
    shutil.copy(REPO / "pramana" / "features.py", mith / "features.py")
    (mith / "profile.tcl").write_text(
        "# Run in Vivado on an open routed design with _rd set to an output dir; then\n"
        "# features.from_profile(<dir>) computes the features (needs profile.json with 'input').\n"
        + tcl.MEASURE_PROCS + tcl.PATHS_DUMP + "\n_paths_dump 200 [file join $_rd paths.tsv]\n"
        + "".join(tcl.safe_report(n, cmd) for n, cmd in prof_mod.REPORTS))
    rule = {"type": "one"} if b["type"] != "two" else \
        {"type": "two", "feature": b["feature"], "threshold": b["threshold"]}
    (mith / "classify.py").write_text(CLASSIFY_PY.format(
        bin_rule=repr(rule), ranges=repr({k: tuple(v) for k, v in kb["feature_ranges"].items()})))
    pols = {}
    if b["type"] == "one":
        pols["all"] = policy_steps(b["all"] or [], kb)
    elif b["type"] == "two":
        pols["low"] = policy_steps(b["low"] or [], kb)
        pols["high"] = policy_steps(b["high"] or [], kb)
    strip = lambda ss: [{k: s[k] for k in ("name", "tcl", "mode", "cmds")} for s in ss]
    singles = [[st] for st in extra_actions(kb)]
    pols = {k: strip(v) for k, v in pols.items()}
    policy_keys = {s["name"] for v in pols.values() for s in v}
    extra = [s for s in strip(policy_steps(singles, kb)) if s["name"] not in policy_keys]
    by_budget = []
    if b["type"] == "one":
        for x in ((kb["policies"].get("by_budget") or {}).get("budgets") or []):
            if x.get("units"):
                by_budget.append({"rel_budget": x["rel_budget"], "lofo": x["lofo_frac_of_oracle"],
                                  "steps": strip(policy_steps(x["units"], kb))})
    (mith / "recipes.py").write_text(RECIPES_PY.format(
        cost_model=repr(kb["cost_model"]), policies=repr(pols), extra=repr(extra),
        by_budget=repr(by_budget)))
    policy = {"bins": b, "policies": pols, "extra_actions": extra, "kb_meta": kb["meta"],
              "by_budget": by_budget}
    (mith / "ladder.py").write_text(LADDER_PY)
    (mith / "policy.json").write_text(json.dumps(policy, indent=1, sort_keys=True))
    md = ["# mithya policy (generated by pramana)", "", f"Bins: {b['type']}", ""]
    for k, ss in pols.items():
        md.append(f"## bin {k}")
        md += [f"- [{s['mode']}] {s['name']}: `{'; '.join(s['tcl'])}`" for s in ss]
    for x in by_budget:
        md.append(f"## budget >= {x['rel_budget']} x route Explore time (LOFO {x['lofo']})")
        md += [f"- [{s['mode']}] {s['name']}" for s in x["steps"]]
    md += ["", "## extra actions (if budget remains)"]
    md += [f"- [{s['mode']}] {s['name']}" for s in extra]
    (mith / "POLICY.md").write_text("\n".join(md) + "\n")
    from . import adapters
    written = adapters.write_all(kb, b, out / "adapters")
    log(f"[compile] maya -> {maya}, mithya -> {mith}, adapters -> {sorted(written)} (bins={b['type']})")
