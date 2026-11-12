"""S8: compile kb.json into the maya (LLM) and mithya (deterministic) bundles.

Compilation is deterministic: the same kb.json gives the same files. It fails if a policy or
recipe refers to evidence that doesn't exist.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

from . import flow, funccheck, profile as prof_mod, profile_ext, ship, tcl
from .campaign import Campaign
from .config import REPO
from .features import FEATURE_DOC
from .space import chain_key, step_factors, step_key


def _nobraces(s: str) -> str:
    return s.replace("{", "(").replace("}", ")")


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", s)[:60]


# Shipping decisions are made in distill (pramana/ship.py, stored as kb["ship"]); these are
# re-exported for the maya and adapter writers.
from .ship import bins, extra_actions, policy_steps, step_mode  # noqa: E402,F401


# --------------------------------------------------------------------------- maya
# Path-share features measured the way profile.py/features.py do (top-200 setup paths,
# -nworst 1), so policy_auto can pick a strategy on the open design. Returns "" for a
# feature with no Tcl measure here (policy_auto then runs both strategies).
FEATURE_TCL = r"""
proc ::pramana::feature {name {n 200}} {
    if {$name eq "congestion_level"} {
        # as pramana/features.py: highest congestion level in report_design_analysis -congestion
        if {[catch {set r [report_design_analysis -congestion -return_string]}]} {return ""}
        set lv 0
        foreach {- l} [regexp -all -inline -line {^\|\s*\w+\s*\|\s*\w+\s*\|\s*(\d+)\s*\|} $r] {
            if {$l > $lv} {set lv $l}
        }
        return $lv
    }
    set paths [get_timing_paths -quiet -setup -max_paths $n -nworst 1]
    set tot [llength $paths]
    if {$tot == 0} {return ""}
    set hit 0
    foreach p $paths {
        set sr ""; set er ""
        catch {set sr [get_property REF_NAME [get_cells -quiet -of_objects [get_property STARTPOINT_PIN $p]]]}
        catch {set er [get_property REF_NAME [get_cells -quiet -of_objects [get_property ENDPOINT_PIN $p]]]}
        switch -- $name {
            carry_frac {
                set refs [get_property REF_NAME [get_cells -quiet -of_objects $p]]
                set h [expr {[lsearch -glob $refs *CARRY*] >= 0}]
            }
            hf_path_frac {
                set h 0
                foreach nt [get_nets -quiet -of_objects $p] {
                    set f [get_property FLAT_PIN_COUNT $nt]
                    if {$f ne "" && $f > 100} {set h 1; break}
                }
            }
            end_bram_frac   {set h [string match RAMB* $er]}
            end_dsp_frac    {set h [string match DSP* $er]}
            end_uram_frac   {set h [string match URAM* $er]}
            end_lutram_frac {set h [expr {[string match RAMD* $er] || [string match RAMS* $er] || [string match SRL* $er]}]}
            start_bram_frac {set h [string match RAMB* $sr]}
            start_dsp_frac  {set h [string match DSP* $sr]}
            cross_clock_frac {set h [expr {[get_property STARTPOINT_CLOCK $p] ne [get_property ENDPOINT_CLOCK $p]}]}
            default {return ""}
        }
        incr hit $h
    }
    return [format %.4f [expr {double($hit) / $tot}]]
}
"""

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
# Time guards. ::pramana::deadline: end of the agent's budget. ::pramana::step_limit: the longest
# one tool call may run (the agent's Vivado server stops a command after it). A policy proc calls
# begin_call; a step is skipped unless its estimate (the previous step's duration, or `need`)
# fits both. The first step of a call has no estimate and always runs.
proc ::pramana::begin_call {} { set ::pramana::call_start [clock seconds]; set ::pramana::last_secs 0 }
proc ::pramana::fits {tag {need 0}} {
    if {[info exists ::pramana::last_secs] && $::pramana::last_secs > $need} {set need $::pramana::last_secs}
    if {![::pramana::time_left $need]} { puts "PRAMANA $tag skipped (does not fit the time left)"; return 0 }
    if {[info exists ::pramana::step_limit] && $::pramana::step_limit > 0 && [info exists ::pramana::call_start]} {
        if {[clock seconds] - $::pramana::call_start + $need > $::pramana::step_limit - 60} {
            puts "PRAMANA $tag skipped (does not fit the $::pramana::step_limit s limit of one call)"; return 0
        }
    }
    return 1
}
proc ::pramana::keep_if_better {tag script {tmpdir /tmp} {need 0}} {
    if {![::pramana::fits $tag $need]} { return 0 }
    set _t0 [clock seconds]
    set ck [file join $tmpdir "pramana_kib_[pid].dcp"]
    write_checkpoint -force $ck
    set before [::pramana::wns]
    if {[catch {uplevel #0 $script} err]} { puts "PRAMANA $tag error: $err" }
    set ::pramana::last_secs [expr {[clock seconds] - $_t0}]
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
        out.append("    ::pramana::begin_call")
        for s in policy_steps(ch or [], kb):
            body = "; ".join(s["tcl"])
            if s["mode"] == "committed":
                out.append(f"    # {s['name']} (committed: never regressed in training)")
                out.append(f"    if {{[::pramana::fits {{{s['name']}}}]}} {{ set _t0 [clock seconds]; "
                           f"{body}; set ::pramana::last_secs [expr {{[clock seconds] - $_t0}}] }}")
            else:
                out.append(f"    ::pramana::keep_if_better {{{s['name']}}} {{{body}}} $tmpdir")
        out.append("}")
    if b["type"] == "two":
        out.append(FEATURE_TCL)
        out.append(f"""# Two-strategy policy: measure {b['feature']} on the open design and run the matching one.
proc ::pramana::policy_auto {{{{tmpdir /tmp}}}} {{
    set v [::pramana::feature {b['feature']}]
    if {{$v eq ""}} {{
        puts "PRAMANA policy_auto: cannot measure {b['feature']} in Tcl; running both strategies"
        ::pramana::policy_low $tmpdir; ::pramana::policy_high $tmpdir; return
    }}
    if {{$v <= {b['threshold']}}} {{
        puts "PRAMANA policy_auto: {b['feature']}=$v <= {b['threshold']} -> policy_low"
        ::pramana::policy_low $tmpdir
    }} else {{
        puts "PRAMANA policy_auto: {b['feature']}=$v > {b['threshold']} -> policy_high"
        ::pramana::policy_high $tmpdir
    }}
}}""")
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
        L.append(f"Tcl (in recipes.tcl): ::pramana::policy_auto measures {b['feature']} "
                 f"({FEATURE_DOC.get(b['feature'], 'see features')}) on the open design and runs "
                 f"::pramana::policy_low or ::pramana::policy_high; "
                 f"::pramana::feature {b['feature']} returns the value alone.")
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
SHIM = REPO / "pramana" / "mithya_shim"     # fixed executor files, copied as-is


def write_mithya(kb: dict, b: dict, mith: Path) -> dict:
    """mithya bundle = the fixed shim + policy.json. Everything learned goes into policy.json;
    the shim's code and protocol constants are identical for every trained KB."""
    mith.mkdir(parents=True, exist_ok=True)
    for old in ("classify.py", "recipes.py", "ladder.py"):    # generated in the old format
        (mith / old).unlink(missing_ok=True)
    for f in SHIM.iterdir():
        if f.suffix in (".py", ".md"):
            shutil.copy(f, mith / f.name)
    (mith / "kib.tcl").write_text(KIB_TCL)
    shutil.copy(REPO / "pramana" / "features.py", mith / "features.py")
    (mith / "profile.tcl").write_text(
        "# Run in Vivado on an open routed design with _rd set to an output dir; then\n"
        "# features.from_profile(<dir>) computes the features (needs profile.json with 'input').\n"
        + tcl.MEASURE_PROCS + tcl.PATHS_DUMP + "\n_paths_dump 200 [file join $_rd paths.tsv]\n"
        + "".join(tcl.safe_report(n, cmd) for n, cmd in prof_mod.REPORTS))
    sh = ship.decide(kb)       # kb["ship"] when distill made it (legacy KBs: computed here)
    pols, default, sel = sh["policies"], sh["default_policy"], sh["selector"]
    extra, by_budget = sh["extra_actions"], sh["by_budget"]
    sd = mith / "selectors"
    if sd.exists():
        shutil.rmtree(sd)
    sd.mkdir()
    (mith / sel["file"]).write_text(sel["code"])
    for old in ("extractors",):
        if (mith / old).exists():
            shutil.rmtree(mith / old)
    (mith / "profile_ext.tcl").write_text(
        "# Extended profile (pramana S7a). Run in Vivado on the open routed design with _rd set\n"
        "# to an output dir; the selector reads these reports with profile.tcl's (extract.py).\n"
        + tcl.MEASURE_PROCS + profile_ext.body())
    policy = {"selector": {k: sel[k] for k in ("name", "file", "model")},
              "default_policy": default, "policies": pols, "extra_actions": extra,
              "by_budget": by_budget, "cost_model": kb["cost_model"],
              "feature_ranges": kb["feature_ranges"], "kb_meta": kb["meta"],
              "selector_scores": {k: sel.get(k) for k in ("lofo", "val")}}
    (mith / "policy.json").write_text(json.dumps(policy, indent=1, sort_keys=True))
    md = ["# mithya policy (learned by pramana)", "",
          f"Selector: {sel['name']} ({sel['file']}), model {json.dumps(sel['model'])}"
          + (f"; held-out score lofo {sel.get('lofo')}, val {sel.get('val')}" if sel.get("lofo") is not None else ""),
          f"Default policy (selector fails / unknown pick): {default}", ""]
    keys = {a: p.get("key") for a, p in ((kb.get("selector") or {}).get("policies") or {}).items()}
    for k, ss in pols.items():
        md.append(f"## policy {k}" + (f" ({keys[k]})" if keys.get(k) else ""))
        md += [f"- [{s['mode']}] {s['name']}: `{'; '.join(s['tcl'])}`" for s in ss]
    for x in by_budget:
        md.append(f"## budget >= {x['rel_budget']} x route Explore time (LOFO {x['lofo']})")
        md += [f"- [{s['mode']}] {s['name']}" for s in x["steps"]]
    md += ["", "## extra actions (if budget remains)"]
    md += [f"- [{s['mode']}] {s['name']}" for s in extra]
    (mith / "POLICY.md").write_text("\n".join(md) + "\n")
    return policy


def check_selector(c: Campaign, kb: dict, mith: Path, log=print) -> None:
    """Emit check: the bundle's selector, run from the bundle, makes on every training design
    the pick distill recorded."""
    want = (kb.get("selector") or {}).get("train_picks")
    if not want:
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location("pramana_emit_selector", mith / "selector.py")
    sel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sel)
    pol = json.loads((mith / "policy.json").read_text())
    s = pol["selector"]
    bad = []
    for did, pick in want.items():
        r = extract_reports(c, did)
        got = sel.predict(mith / s["file"], s["model"], r, c.features(did), pol["policies"])
        if got != pick:
            bad.append(did)
    if bad:
        raise SystemExit(f"emit: bundled selector {s['name']} picks differently on {bad}")
    log(f"[compile] selector {s['name']}: same picks as in training on {len(want)} designs")


def extract_reports(c: Campaign, did: str) -> dict:
    import importlib.util
    spec = importlib.util.spec_from_file_location("pramana_emit_extract",
                                                  REPO / "pramana" / "mithya_shim" / "extract.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.load_reports(c.profile_dir(did), profile_ext.ext_dir(c.cfg, c.design(did)))


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
    write_mithya(kb, b, mith)
    check_selector(c, kb, mith, log)
    from . import adapters
    written = adapters.write_all(kb, b, out / "adapters")
    log(f"[compile] maya -> {maya}, mithya -> {mith}, adapters -> {sorted(written)} (bins={b['type']})")


if __name__ == "__main__":
    # Rebuild a mithya bundle from a saved KB (e.g. a snapshot), without a campaign:
    #   python -m pramana.compile <kb.json> <bundle dir>
    import sys
    _kb = json.loads(Path(sys.argv[1]).read_text())
    _p = write_mithya(_kb, bins(_kb), Path(sys.argv[2]))
    print(f"mithya bundle -> {sys.argv[2]} (selector={_p['selector']['name']})")
