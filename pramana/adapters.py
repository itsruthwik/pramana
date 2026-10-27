"""Render a pramana KB into the knowledge formats of two published LLM timing-closure agents.

Research question: can a generated KB stand in for a hand-written one? Each adapter writes the
files the agent loads, plus a README that says where every field came from and what had no
equivalent.

  latch/  LATCH (arXiv 2610.16625, github.com/aprilaihub/LATCH, Apache-2.0). Format read from
          commit c53a8bf5f24159d527eb4508dc3cdda6144ca2aa:
            experiments/ablation/runtime/knowledge/{loader,projector,schema}.py and
            experiments/ablation/runtime/analysis/dynamic_loop.py (_compact_knowledge_context).
          The KB is one JSON object {schema_version, note, flow_cards: {<flow id>: card}}; a card
          needs id, display_name, description, use_when, avoid_when, ceiling_note. Only
          flow_cards (strings cut at 900 chars, lists at 20 items) reach the LLM, as
          `knowledge_context`, which the system prompt labels heuristic_prior evidence. The
          LLM can only pick from the nine coded flows in `allowed_flows`, so cards are keyed
          by those nine ids.
          recipe_specs.json uses LATCH's RecipeSpec format
          (optimizer_pipeline/capabilities/intervention/recipe.py). It is run by the
          `synthesized_recipe` flow, which only allows the primitives in its catalogue.
  pact/   PACT (arXiv 2608.23602v2). Its code is NOT released: the anonymous repository
          (anonymous.4open.science/r/PACT-552C) has only a "code release coming soon" README.
          The format here is rebuilt from the paper (Sec. III, Table II, Algorithm 1) and has
          NOT been checked against code.

Deterministic: the same kb gives byte-identical files. Every number comes from kb.json.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# Bound to the module, not to its names: compile.run imports this module, so a
# `from .compile import ...` here would be circular.
from . import compile as _cmp
from . import corpus
from .space import chain_key, step_factors

LATCH_REPO = "https://github.com/aprilaihub/LATCH"
LATCH_COMMIT = "c53a8bf5f24159d527eb4508dc3cdda6144ca2aa"
PACT_PAPER = "arXiv:2608.23602v2"
PACT_REPO = "https://anonymous.4open.science/r/PACT-552C"
LATCH_MAX_STR, LATCH_MAX_LIST = 900, 20
LATCH_CARD_FIELDS = ("id", "display_name", "description", "use_when", "avoid_when", "ceiling_note")
PACT_EPS_NS = 0.0005   # PACT accepts a trial only if setup WNS improves by > 0.5 ps (Sec. III-E)


def _dump(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def _fmt(x) -> str:
    return "n/a" if x is None else f"{x:+g}" if isinstance(x, (int, float)) else str(x)


def _actions(kb: dict) -> dict[str, dict]:
    return {a["factor"]: a for a in kb["actions"]}


def _cond_by_factor(kb: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for c in kb["conditions"]:
        out.setdefault(c["factor"], []).append(c)
    return out


def _negative_by_factor(kb: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for n in kb["negative"]:
        out.setdefault(n["factor"], n)
    return out


def _policy_chains(b: dict) -> dict[str, list[list[dict]]]:
    if b["type"] == "one":
        return {"all": b["all"] or []}
    if b["type"] == "two":
        return {"low": b["low"] or [], "high": b["high"] or []}
    return {}


def _profile_line(a: dict) -> str:
    p = a["profile"]
    return (f"median dWNS {_fmt(p['median_d_wns'])} ns over {p['n_designs']} designs / "
            f"{p.get('n_families', 'n/a')} families, P(gain) {p['p_gain']}, P(regress) "
            f"{p['p_regress']}, rel. cost {p['rel_cost']}, grade {a['grade']}")


def _best_design(a: dict) -> tuple[str, float] | None:
    pd = a.get("per_design") or {}
    if not pd:
        return None
    d = max(sorted(pd), key=lambda k: pd[k])
    return d, pd[d]


def _factor_step(factor: str) -> dict | None:
    """'phys_opt:directive=Explore' -> step dict (compile.extra_actions convention: a route
    directive is measured with +ripup). Option factors -> {'opts': {...}}."""
    if factor.count(":") != 1:
        return None
    cmd, rest = factor.split(":", 1)
    if rest.startswith("directive="):
        st = {"cmd": cmd, "directive": rest.split("=", 1)[1]}
        if cmd == "route":
            st["opts"] = {"+ripup": True}
        return st
    if rest.startswith(("-", "+")):
        k, _, v = rest.partition("=")
        return {"cmd": cmd, "opts": {k: v if v else True}}
    return None


# --------------------------------------------------------------------------- LATCH
# The nine flows in LATCH's study catalogue (configs/frontier_expansion_default.json
# allowed_flows). The mechanism text summarises what LATCH's code runs
# (optimizer_pipeline/backends/xilinx/legacy.py run_test_*). It is NOT copied from LATCH's
# hand-written cards, so their judgements don't leak into the generated KB.
#   same:       pramana factors that run the same Vivado operation as the flow's core step
#   related:    pramana factors with a similar mechanism (other directive / tool)
#   components: pramana option factors that the flow combines into one phys_opt pass
LATCH_FLOWS: dict[str, dict] = {
    "phys_opt": {
        "display_name": "phys_opt_design -directive Explore (twice, re-route between)",
        "mechanism": "phys_opt_design -directive Explore; route_design; phys_opt_design "
                     "-directive Explore.",
        "same": ["phys_opt:directive=Explore"],
        "related": ["phys_opt:directive=AggressiveExplore", "phys_opt:directive=ExploreWithHoldFix",
                    "phys_opt:directive=ExploreWithAggressiveHoldFix"],
        "components": [], "phys_opt": True},
    "retime": {
        "display_name": "phys_opt_design -directive AlternateFlowWithRetiming",
        "mechanism": "place_design (incremental); phys_opt_design -directive "
                     "AlternateFlowWithRetiming; route_design.",
        "same": ["phys_opt:directive=AlternateFlowWithRetiming"],
        "related": ["phys_opt:directive=AddRetime"],
        "components": [], "phys_opt": True},
    "fanout": {
        "display_name": "High-fanout driver splitting (RapidWright)",
        "mechanism": "RapidWright FanOutOptimization splits the drivers of up to max_nets "
                     "critical nets with fanout >= 100; Vivado re-routes.",
        "same": [],
        "related": ["phys_opt:directive=AggressiveFanoutOpt", "phys_opt:directive=AlternateReplication",
                    "phys_opt:-fanout_opt", "phys_opt:-force_replication_on_nets"],
        "components": [], "phys_opt": False},
    "congestion_spread": {
        "display_name": "phys_opt placement/critical-cell/clock/SLR pass",
        "mechanism": "phys_opt_design -placement_opt -critical_cell_opt -clock_opt "
                     "-slr_crossing_opt; route_design (congestion report captured only).",
        "same": [], "related": [],
        "components": ["phys_opt:-placement_opt", "phys_opt:-critical_cell_opt",
                       "phys_opt:-clock_opt", "phys_opt:-slr_crossing_opt"], "phys_opt": True},
    "memory_path_opt": {
        "display_name": "phys_opt BRAM/DSP/URAM register pass",
        "mechanism": "phys_opt_design with bram/dsp/uram register opts, bram_enable_opt, "
                     "critical_pin_opt, equ_drivers_opt, placement_opt; route_design; cleanup.",
        "same": [], "related": [],
        "components": ["phys_opt:-bram_register_opt", "phys_opt:-dsp_register_opt",
                       "phys_opt:-uram_register_opt", "phys_opt:-bram_enable_opt",
                       "phys_opt:-critical_pin_opt", "phys_opt:-equ_drivers_opt",
                       "phys_opt:-placement_opt"], "phys_opt": True},
    "hardblock_relocate": {
        "display_name": "phys_opt around hard blocks",
        "mechanism": "phys_opt_design placement_opt + critical_cell_opt + dsp/bram/uram register "
                     "opts; route_design; cleanup. Hard blocks themselves are not moved.",
        "same": [], "related": [],
        "components": ["phys_opt:-placement_opt", "phys_opt:-critical_cell_opt",
                       "phys_opt:-dsp_register_opt", "phys_opt:-bram_register_opt",
                       "phys_opt:-uram_register_opt"], "phys_opt": True},
    "net_swap": {
        "display_name": "phys_opt pin swap / equivalent-driver rewiring",
        "mechanism": "phys_opt_design -critical_pin_opt -equ_drivers_opt -placement_opt; "
                     "route_design; phys_opt_design -critical_pin_opt -equ_drivers_opt -routing_opt.",
        "same": [], "related": [],
        "components": ["phys_opt:-critical_pin_opt", "phys_opt:-equ_drivers_opt",
                       "phys_opt:-placement_opt", "phys_opt:-routing_opt"], "phys_opt": True},
    "lut_cone": {
        "display_name": "LUT cone merging (RapidWright)",
        "mechanism": "RapidWright LUTInputConeOpt merges chained LUTs on the worst paths; "
                     "re-route without re-placement.",
        "same": [], "related": [], "components": [], "phys_opt": False},
    "auto_pblock": {
        "display_name": "Whole-design pblock and re-implementation",
        "mechanism": "Unplace the whole design, apply one hard pblock sized from utilisation, "
                     "re-place and re-route.",
        "same": [], "related": [], "components": [], "phys_opt": False},
}

# LATCH RecipeSpec primitive allowlist (PrimitiveCatalog.default at LATCH_COMMIT).
LATCH_PRIMS = {
    "place_design": ("vivado.place_design", ("Default", "Explore")),
    "phys_opt_design": ("vivado.phys_opt_design", ("Explore", "AddRetime", "AlternateFlowWithRetiming")),
    "route_design": ("vivado.route_design", ("Default", "Explore")),
}
LATCH_MACROS = {"route_design -unroute": "macro.unroute_design",
                "place_design -unplace": "macro.unplace_design"}


def _latch_card(fid: str, spec: dict, kb: dict, used_in_policy: dict[str, list[str]]) -> tuple[dict, dict]:
    acts, conds, neg = _actions(kb), _cond_by_factor(kb), _negative_by_factor(kb)
    use, avoid, ceiling, ev = [], [], [], {}
    matched = []
    for kind in ("same", "related", "components"):
        for f in spec[kind]:
            if f in acts or f in neg or f in conds:
                matched.append((kind, f))
    for kind, f in matched:
        a = acts.get(f)
        if a:
            p = a["profile"]
            line = f"[{kind}] {f}: {_profile_line(a)}"
            (use if p["median_d_wns"] > 0 and p["p_gain"] >= 0.5 else avoid).append(line)
            if p.get("incremental_median_d_wns") is not None:
                ceiling.append(f"{f} after other steps had run: median dWNS "
                               f"{_fmt(p['incremental_median_d_wns'])} ns (n={p['incremental_n']}).")
            bd = _best_design(a)
            if bd:
                ceiling.append(f"{f} best single design: {_fmt(bd[1])} ns ({bd[0]}).")
            ev[f] = {"match": kind, "grade": a["grade"], "ship_mode": a["ship_mode"],
                     "n_evidence_runs": len(a["evidence"]),
                     **{k: p.get(k) for k in ("median_d_wns", "mean_d_wns", "p_gain", "p_regress",
                                              "rel_cost", "n_designs", "n_families",
                                              "incremental_median_d_wns", "incremental_n")}}
        for c in conds.get(f, []):
            if c["status"] == "condition":
                use.append(f"[{kind}] {f} {c['rule']} (held-out accuracy {c['lofo_acc']} vs "
                           f"{c['lofo_majority_acc']} baseline, grade {c.get('grade')})")
        if f in neg:
            avoid.append(f"[{kind}] {f}: {neg[f]['why']} (cut on {neg[f]['cut_on']} designs)")
            ev.setdefault(f, {"match": kind})["pruned_on_designs"] = neg[f]["cut_on"]
        for pol in used_in_policy.get(f, []):
            use.append(f"[{kind}] {f} is a step of pramana's learned policy: {pol}")
    if spec["phys_opt"]:
        for s in kb["signals"]:
            avoid.append(f"{s['signal']} on the previous phys_opt step: P(gain) "
                         f"{s['p_gain_given_zero']} (n={s['n_zero']}) vs {s['p_gain_given_nonzero']} "
                         f"(n={s['n_nonzero']}) otherwise; {s['use']}.")
    notes = [(f, kb.get("notes", {}).get("action:" + f)) for _, f in matched]
    notes = [(f, n) for f, n in notes if n]
    m = kb["meta"]
    if matched:
        desc = (f"{spec['mechanism']} Pramana prior (dWNS in ns from the input routed checkpoint, "
                f"positive = better; {len(m['designs'])} training designs, {m['part']}, Vivado "
                f"{m['vivado']}): see use_when/avoid_when. "
                + (f"Mechanism note on {notes[0][0]}: {notes[0][1]}" if notes else ""))
    else:
        desc = (f"{spec['mechanism']} Pramana has NO measurement of this flow or of any of its "
                f"component options: no prior either way"
                + (" (avoid_when holds only pramana's generic phys_opt signal)."
                   if spec["phys_opt"] and kb["signals"] else "."))
        ceiling.append("No pramana evidence; the ceiling is unknown.")
    # LATCH's _compact_knowledge_context cuts strings at 900 chars and lists at 20 items;
    # cap here so what we write is exactly what its LLM sees.
    cap = lambda s: s if len(s) <= LATCH_MAX_STR else s[:LATCH_MAX_STR - 3] + "..."
    card = {"id": fid, "display_name": spec["display_name"], "description": cap(desc.strip()),
            "use_when": [cap(s) for s in use[:LATCH_MAX_LIST]],
            "avoid_when": [cap(s) for s in avoid[:LATCH_MAX_LIST]],
            "ceiling_note": cap(" ".join(ceiling))}
    return card, {"match": sorted({k for k, _ in matched}) or ["none"], "factors": ev}


def _latch_recipe(name: str, tcl: list[str]) -> tuple[list[dict] | None, str]:
    steps = []
    for cmd in tcl:
        cmd = cmd.strip()
        if cmd in LATCH_MACROS:
            steps.append({"primitive": LATCH_MACROS[cmd], "args": {}})
            continue
        mt = re.fullmatch(r"(place_design|phys_opt_design|route_design)(?: -directive (\S+))?", cmd)
        if not mt:
            return None, f"command not expressible as a LATCH primitive: {cmd!r}"
        prim, allowed = LATCH_PRIMS[mt.group(1)]
        d = mt.group(2) or "Default"
        if d not in allowed:
            return None, f"{mt.group(1)} directive {d} is not in LATCH's allowlist {list(allowed)}"
        steps.append({"primitive": prim, "args": {"directive": d}})
    if len(steps) > 8:
        return None, "more than 8 steps (LATCH RecipeValidator max_steps)"
    for i, s in enumerate(steps, 1):
        s["step_id"] = f"step_{i:02d}"
        s["rationale"] = f"pramana unit {name}"
    return steps, ""


def write_latch(kb: dict, b: dict, out: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    m = kb["meta"]
    chains = _policy_chains(b)
    used_in_policy: dict[str, list[str]] = {}
    for bn, ch in chains.items():
        units = [u for u in ch if u]
        for i, u in enumerate(units, 1):
            for st in u:
                for f in step_factors(st):
                    used_in_policy.setdefault(f, []).append(
                        f"bin '{bn}' unit {i} of {len(units)} ({chain_key(u)})")
    used_in_policy = {k: sorted(set(v)) for k, v in used_in_policy.items()}
    cards, prov = {}, {}
    for fid in sorted(LATCH_FLOWS):
        cards[fid], prov[fid] = _latch_card(fid, LATCH_FLOWS[fid], kb, used_in_policy)
    manual = {
        "schema_version": "flow_cards_c2_deploy.v1",
        "note": (f"Generated by pramana from kb.json of campaign {m['campaign']} "
                 f"({len(m['designs'])} designs, {len(m['families'])} families, {m['n_runs']} runs, "
                 f"{m['part']}, Vivado {m['vivado']}); replaces LATCH's hand-written "
                 f"reference_manual_final.json. One card per LATCH flow; flows pramana never "
                 f"measured say so. Only flow_cards reach the LLM."),
        "flow_cards": cards,
    }
    # recipe specs: every policy unit and extra action, where LATCH's primitives allow it
    units = []
    for bn, ch in chains.items():
        for i, s in enumerate(_cmp.policy_steps(ch, kb), 1):
            units.append((f"policy_{bn}_{i:02d}", s))
    pol_names = {s["name"] for _, s in units}
    for s in _cmp.policy_steps([[st] for st in _cmp.extra_actions(kb)], kb):
        if s["name"] not in pol_names:
            units.append((f"extra_{len(units) + 1:02d}", s))
    recipes, untrans = [], []
    for rid, s in units:
        steps, why = _latch_recipe(s["name"], s["tcl"])
        if steps is None:
            untrans.append({"recipe_id": f"pramana_{rid}", "pramana_unit": s["name"], "tcl": s["tcl"],
                            "reason": why})
            continue
        risk = [f"pramana mode {s['mode']}: " + ("never regressed in training" if s["mode"] == "committed"
                                                  else "run keep-if-better (may regress)")]
        if b["type"] == "two":
            risk.append(f"pramana splits designs on {b['feature']} at {b['threshold']}; LATCH "
                        f"has no design binning, choose the bin's recipes yourself")
        recipes.append({"recipe_id": f"pramana_{rid}", "summary": f"pramana unit {s['name']}",
                        "source": f"pramana:{m['campaign']}", "risk_notes": risk, "steps": steps})
    for r in kb["recipes"]:
        untrans.append({"recipe_id": f"pramana_recipe_{r['from_design']}_{r['name']}",
                        "pramana_unit": f"tcl:{r['name']}", "tcl": [r["tcl"]],
                        "reason": "custom Tcl; LATCH forbids run_tcl in synthesized recipes"})
    specs = {"schema_version": "pramana.latch_recipe_specs.v1",
             "note": ("LATCH RecipeSpec dicts (optimizer_pipeline/capabilities/intervention/"
                      "recipe.py); run each with run_test_mode(flow='synthesized_recipe', "
                      "optimizer_params={'recipe_spec': <recipe>})."),
             "recipes": recipes, "untranslatable": untrans}
    files = {"latch/reference_manual_pramana.json": out / "reference_manual_pramana.json",
             "latch/recipe_specs.json": out / "recipe_specs.json",
             "latch/provenance.json": out / "provenance.json",
             "latch/README.md": out / "README.md"}
    files["latch/reference_manual_pramana.json"].write_text(_dump(manual))
    files["latch/recipe_specs.json"].write_text(_dump(specs))
    files["latch/provenance.json"].write_text(_dump(
        {"latch_repo": LATCH_REPO, "latch_commit": LATCH_COMMIT, "kb_meta": m, "cards": prov}))
    mapped = {f for spec in LATCH_FLOWS.values() for k in ("same", "related", "components") for f in spec[k]}
    no_eq = [a for a in kb["actions"] if a["factor"] not in mapped]
    files["latch/README.md"].write_text(_latch_readme(kb, cards, prov, recipes, untrans, no_eq))
    return files


def _latch_readme(kb, cards, prov, recipes, untrans, no_eq) -> str:
    m = kb["meta"]
    L = ["# pramana KB in LATCH's format", "",
         f"Generated by `pramana.adapters` from `kb.json` of campaign `{m['campaign']}` "
         f"({len(m['designs'])} designs, {len(m['families'])} families, {m['n_runs']} runs, "
         f"{m['part']}, Vivado {m['vivado']}, KB created {m['created']}).",
         f"Target: LATCH {LATCH_REPO} at commit `{LATCH_COMMIT}` (code Apache-2.0).", "",
         "## Files", "",
         "- `reference_manual_pramana.json`: drop-in replacement for LATCH's hand-written "
         "`experiments/ablation/runtime/knowledge/data/reference_manual_final.json` (S3 condition, "
         "`knowledge_mode=reference_manual`). Same schema (`flow_cards_c2_deploy.v1`): "
         "`flow_cards.<flow id>.{id, display_name, description, use_when, avoid_when, ceiling_note}`.",
         "- `recipe_specs.json`: pramana's policy units and extra actions as LATCH `RecipeSpec` "
         "dicts for the `synthesized_recipe` flow, plus the units that can't be expressed.",
         "- `provenance.json`: for each card, the pramana factors behind it and their numbers.", "",
         "## Field mapping (card <- kb.json)", "",
         "- `id`: LATCH flow id (one of the 9 in `allowed_flows`). The LLM can only select these.",
         "- `display_name`, and the first sentence of `description`: what LATCH's code runs for the "
         "flow (written for this adapter, not copied from LATCH's cards).",
         "- `description`, rest: the `notes[\"action:<factor>\"]` mechanism note of the first matched factor.",
         "- `use_when`: matched `actions[]` with median dWNS > 0 and P(gain) >= 0.5 (from `profile`), "
         "`conditions[]` with status `condition`, and membership in `policies` (via `bins(kb)`).",
         "- `avoid_when`: other matched `actions[]`, matched `negative[]` (pruned factors), and "
         "`signals[]` for flows that run phys_opt_design.",
         "- `ceiling_note`: `profile.incremental_median_d_wns` and the best `per_design` value.",
         "- Match tags: `[same]` = same Vivado operation as the flow's core step; `[related]` = "
         "similar mechanism (another directive or tool); `[components]` = an option the flow combines.", "",
         "## Coverage", "", "flow | match | pramana factors", "--- | --- | ---"]
    for fid in sorted(cards):
        L.append(f"{fid} | {', '.join(prov[fid]['match'])} | {', '.join(sorted(prov[fid]['factors'])) or '-'}")
    L += ["", f"### pramana actions with no LATCH flow ({len(no_eq)})", "",
          "LATCH has no flow for these (mostly full re-place, opt and route directives). They don't "
          "reach the LATCH LLM. Those with allowlisted directives appear in `recipe_specs.json`.", ""]
    L += [f"- {a['factor']}: {_profile_line(a)}" for a in no_eq]
    L += ["", "### Other kb.json content with no LATCH field", "",
          "- `policies` / `bins` order and the portfolio: LATCH's frontier search picks the order. Only "
          "membership is stated in `use_when`, and the units are in `recipe_specs.json`.",
          "- `conditions[]` that are not `condition` status (weak, failed the shuffle test): dropped.",
          f"- `recipes[]` ({len(kb['recipes'])} custom Tcl recipes): LATCH forbids custom Tcl; listed "
          "under `untranslatable` in `recipe_specs.json`.",
          "- `cost_model`, `feature_ranges`, `design_features`, `hypotheses`, `pruning`, `llm_spend`: "
          "no field.",
          "- LATCH's older card fields `typical_fmax_delta_mhz`, `runtime_class` and `risk_level` are not "
          "produced: pramana measures dWNS (ns), not MHz, and has no runtime buckets.", "",
          f"## recipe_specs.json: {len(recipes)} translated, {len(untrans)} untranslatable", ""]
    L += [f"- {r['recipe_id']}: " + " ; ".join(f"{s['primitive']}{(' ' + s['args']['directive']) if s['args'] else ''}"
                                               for s in r["steps"]) for r in recipes]
    L += [f"- (untranslatable) {u['recipe_id']}: {u['reason']}" for u in untrans]
    L += ["", "See docs/adapters.md for how to run LATCH with these files.", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------- PACT
# Table II of the paper (wording shortened). group: local | global | diagnostic.
PACT_FAMILIES = {
    "fanout_replication": {"group": "local", "backend": ["V", "RW"],
                           "trigger": "High fanout, route-dominated delay; replicate driver, split sinks, repair routing."},
    "local_rerouting": {"group": "local", "backend": ["V"],
                        "trigger": "High route delay on critical nets; unroute and reroute a bounded window."},
    "local_replacement": {"group": "local", "backend": ["V", "RW"],
                          "trigger": "Cell-local path delay; move cells or endpoints, repair affected routes."},
    "logic_restructuring": {"group": "local", "backend": ["V", "RW"],
                            "trigger": "LUT-dominated critical cone; LUT-cone or input-pin transform."},
    "floorplanning": {"group": "local", "backend": ["V", "RW"],
                      "trigger": "Congestion or long inter-region paths; adjust a pblock, reimplement region."},
    "directive_sweep": {"group": "global", "backend": ["V"],
                        "trigger": "Weak or diffuse evidence; sweep post-route opt and routing directives."},
    "replace_route": {"group": "global", "backend": ["V"],
                      "trigger": "Local candidates exhausted; timing-driven place-and-route from current DCP."},
    "clock_retargeting": {"group": "global", "backend": ["V"],
                          "trigger": "Need stronger pressure; tighten temporarily, validate under original clock."},
    "control_probing": {"group": "diagnostic", "backend": ["V", "RW"],
                        "trigger": "Import, report or inspect objects; never mutates the checkpoint."},
}
# pramana feature -> the PACT timing-profile item it approximates (Sec. III-B list).
PACT_PROFILE = {
    "wns_frac": "worst negative slack", "tns_per_fe_frac": "total negative slack",
    "fe_frac": "total negative slack", "near_crit_frac": "total negative slack",
    "logic_frac": "logic and routing delay", "levels_mean": "logic and routing delay",
    "levels_max": "logic and routing delay", "carry_frac": "logic and routing delay",
    "hf_path_frac": "high-fanout nets", "log10_max_fanout": "high-fanout nets",
    "congestion_level": "routing problems",
    "end_bram_frac": "critical start and end points", "end_dsp_frac": "critical start and end points",
    "end_uram_frac": "critical start and end points", "end_lutram_frac": "critical start and end points",
    "start_bram_frac": "critical start and end points", "start_dsp_frac": "critical start and end points",
    "cross_clock_frac": "critical start and end points",
}
PACT_CHECKS = ["reopenable", "fully_routed", "no_routing_errors", "hold", "pulse_width",
               "min_period", "design_consistency"]


def _pact_family_factor(factor: str) -> str:
    cmd = factor.split(":", 1)[0]
    if cmd in ("place", "opt") or factor == "route:+ripup":
        return "replace_route"   # re-place (and re-route) from the netlist / full re-route
    return "directive_sweep"


def _pact_family_tcl(tcl: str) -> str:
    if "set_clock_uncertainty" in tcl:
        return "clock_retargeting"
    if "-force_replication_on_nets" in tcl:
        return "fanout_replication"
    if "place_design" in tcl:
        return "replace_route"
    return "directive_sweep"


def _pact_bottleneck(factor: str, conds: dict, family: str) -> dict:
    ok = [c for c in conds.get(factor, []) if c["status"] == "condition"]
    weak = [c for c in conds.get(factor, []) if c["status"] != "condition"]

    def pred(c):
        return {"feature": c["feature"], "op": "<=" if c["side"] < 0 else ">",
                "threshold": c["threshold"], "pact_profile_item": PACT_PROFILE.get(c["feature"]),
                "lofo_acc": c["lofo_acc"], "lofo_majority_acc": c["lofo_majority_acc"],
                "perm_p": c.get("perm_p"), "grade": c.get("grade"), "status": c["status"]}
    return {"default_trigger": PACT_FAMILIES[family]["trigger"],
            "predicates": [pred(c) for c in ok], "unconfirmed_predicates": [pred(c) for c in weak]}


def write_pact(kb: dict, b: dict, out: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    m = kb["meta"]
    conds = _cond_by_factor(kb)
    recipes, cases = [], []
    for a in kb["actions"]:
        f, p = a["factor"], a["profile"]
        fam = _pact_family_factor(f)
        st = _factor_step(f)
        tcl = _cmp.policy_steps([[st]], kb)[0]["tcl"] if st else []
        rid = "pramana:" + f
        recipes.append({
            "recipe_id": rid, "family": fam, "bottleneck": _pact_bottleneck(f, conds, fam),
            "action": {"family": fam, "operation": f.split(":", 1)[0] + "_design",
                       "targets": "whole design", "backend": "V",
                       "params": {k: v for k, v in (st or {}).items() if k != "cmd"},
                       "tcl": tcl, "preconditions": ["accepted checkpoint is placed and routed"],
                       "required_checks": "validation_policy",
                       "est_cost": {"rel_cost": p["rel_cost"], "commands": [c.split()[0] for c in tcl]}},
            "prior": {k: p.get(k) for k in ("median_d_wns", "mean_d_wns", "p_gain", "p_regress",
                                            "validity", "n_designs", "n_families", "n_runs",
                                            "incremental_median_d_wns", "incremental_n")},
            "provenance": {"pramana_factor": f, "grade": a["grade"], "ship_mode": a["ship_mode"],
                           "n_evidence_runs": len(a["evidence"]),
                           "mechanism_note": kb.get("notes", {}).get("action:" + f)}})
        for d in sorted(a.get("per_design") or {}):
            v = a["per_design"][d]
            cases.append({"case_id": f"{d}|{rid}", "design": d, "family": corpus.family_of(d),
                          "action_ref": rid, "outcome": "accept" if v > PACT_EPS_NS else "reject",
                          "d_wns_ns": v, "source": "pramana per_design"})
    for r in kb["recipes"]:
        if not r.get("own_valid"):
            continue
        fam = _pact_family_tcl(r["tcl"])
        rid = f"pramana:recipe:{r['from_design']}:{r['name']}"
        recipes.append({
            "recipe_id": rid, "family": fam,
            "bottleneck": {"default_trigger": PACT_FAMILIES[fam]["trigger"], "predicates": [],
                           "unconfirmed_predicates": [], "found_on": r["from_design"]},
            "action": {"family": fam, "operation": "tcl", "targets": "see tcl", "params": {},
                       "backend": "V", "tcl": [r["tcl"]],
                       "preconditions": ["accepted checkpoint is placed and routed"],
                       "required_checks": "validation_policy", "est_cost": {}},
            "prior": {"own_d_wns": r.get("own_d_wns"), "repeat_d_wns": r.get("repeat_d_wns"),
                      "helped_designs": r.get("helped_designs", []), "scope": r.get("scope")},
            "provenance": {"pramana_recipe": r["name"], "from_design": r["from_design"],
                           "ship_mode": r.get("ship_mode"), "rationale": r.get("rationale")}})
        for d, v in sorted([(r["from_design"], r.get("own_d_wns"))] + sorted((r.get("transfer") or {}).items())):
            if v is None:
                continue
            cases.append({"case_id": f"{d}|{rid}", "design": d, "family": corpus.family_of(d),
                          "action_ref": rid, "outcome": "accept" if v > PACT_EPS_NS else "reject",
                          "d_wns_ns": v, "source": "pramana recipe own/transfer"})
    fallback = []
    for bn, ch in _policy_chains(b).items():
        for i, s in enumerate(_cmp.policy_steps(ch, kb), 1):
            fallback.append({"bin": bn, "order": i, "unit": s["name"], "tcl": s["tcl"],
                             "mode": s["mode"],
                             "family": "replace_route" if {"place", "opt"} & set(s["cmds"])
                             else "directive_sweep"})
    lib = {
        "format": ("pact.recipe_library, rebuilt from " + PACT_PAPER + " Sec. III-B/C/E, Table II, "
                   "Algorithm 1. UNVERIFIED: PACT's code is not released (" + PACT_REPO + ")."),
        "source": {"generator": "pramana.adapters", "campaign": m["campaign"], "part": m["part"],
                   "vivado": m["vivado"], "designs": m["designs"], "families": m["families"],
                   "n_runs": m["n_runs"], "kb_created": m["created"], "unit": m.get("unit")},
        "units": "d_wns in ns from the input routed checkpoint, positive = better; rel_cost = step "
                 "time / that design's route Explore time",
        "families": PACT_FAMILIES,
        "profile_items": PACT_PROFILE,
        "feature_definitions": kb.get("feature_definitions", {}),
        "validation_policy": {"required_checks": PACT_CHECKS, "accept_if": "setup WNS > best + eps",
                              "eps_ns": PACT_EPS_NS, "score_under": "original clock constraints"},
        "recipes": recipes,
        "fallback_order": fallback,
        "bins": ({"type": b["type"]} if b["type"] != "two" else
                 {"type": "two", "feature": b["feature"], "threshold": b["threshold"],
                  "pact_profile_item": PACT_PROFILE.get(b["feature"])}),
        "contraindications": [{"pramana_factor": n["factor"], "reason": n["why"], "cut_on": n["cut_on"]}
                              for n in kb["negative"]],
        "suppress_rules": [{"signal": s["signal"], "meaning": s["meaning"], "use": s["use"],
                            "p_gain_given_zero": s["p_gain_given_zero"], "n_zero": s["n_zero"],
                            "p_gain_given_nonzero": s["p_gain_given_nonzero"], "n_nonzero": s["n_nonzero"]}
                           for s in kb["signals"]],
        "cost_model": kb.get("cost_model", {}),
    }
    files = {"pact/recipe_library.json": out / "recipe_library.json",
             "pact/prior_cases.jsonl": out / "prior_cases.jsonl",
             "pact/README.md": out / "README.md"}
    files["pact/recipe_library.json"].write_text(_dump(lib))
    files["pact/prior_cases.jsonl"].write_text(
        "".join(json.dumps(c, sort_keys=True) + "\n" for c in cases))
    files["pact/README.md"].write_text(_pact_readme(kb, lib, cases))
    return files


def _pact_readme(kb, lib, cases) -> str:
    m = kb["meta"]
    by_fam: dict[str, int] = {}
    for r in lib["recipes"]:
        by_fam[r["family"]] = by_fam.get(r["family"], 0) + 1
    L = ["# pramana KB in PACT's recipe-library format (UNVERIFIED)", "",
         f"Generated by `pramana.adapters` from `kb.json` of campaign `{m['campaign']}` "
         f"({len(m['designs'])} designs, {m['n_runs']} runs, {m['part']}, Vivado {m['vivado']}).", "",
         f"**Unverified against code.** PACT ({PACT_PAPER}) says its source is in {PACT_REPO}. When "
         "checked, that repository had only a README (\"code release coming soon\", licence \"to be "
         "determined\"). The format below follows the paper's description (Sec. III, Table II, "
         "Algorithm 1). The paper gives no file schema, so field names are this adapter's choice.",
         "", "## Paper to file", "",
         "- Recipe library P (\"fixed bottleneck-to-action mappings\") -> `recipe_library.json` "
         "`recipes[]`: `bottleneck` (trigger) -> `action` (a typed instance of one family: operation, "
         "targets, params, backend, preconditions, required checks, estimated cost; Sec. III-C).",
         "- The 9 action families (Table II) -> `families`.",
         "- Timing profile (Sec. III-B) -> `profile_items`: each pramana feature used in a trigger, "
         "mapped to the profile item it approximates.",
         "- Validation and acceptance (Sec. III-E) -> `validation_policy` (eps = 0.5 ps).",
         "- Evidence record M -> `prior_cases.jsonl`. In the main experiments PACT's M holds only "
         "the current run. These files match the leave-one-out \"prior evidence\" setting. A case is "
         "`accept` when d_wns > eps.",
         "- \"M marks it as duplicate or contraindicated\" -> `contraindications` (from `negative[]`) "
         "and `suppress_rules` (from `signals[]`).", "",
         "## Field mapping (kb.json -> library)", "",
         "- `actions[]` -> one recipe each. `profile` -> `prior`; `grade`, `ship_mode`, `evidence` -> "
         "`provenance`; Tcl from `compile.policy_steps`; `rel_cost` -> `action.est_cost`.",
         "- Family: place/opt directives and route `+ripup` -> `replace_route`; phys_opt/route "
         "directives -> `directive_sweep`.",
         "- `conditions[]` -> `bottleneck.predicates` (status `condition`) or "
         "`unconfirmed_predicates` (weak).",
         "- `recipes[]` (own_valid) -> recipes. Family from Tcl: set_clock_uncertainty -> "
         "`clock_retargeting`; -force_replication_on_nets -> `fanout_replication`.",
         "- `policies` via `bins(kb)` -> `fallback_order` (and `bins`).",
         "- `actions[].per_design`, `recipes[].transfer` -> `prior_cases.jsonl` (family label from "
         "`corpus.family_of`, not from kb.json).",
         "- `cost_model`, `feature_definitions` -> copied.", "",
         "## Recipes per family", "", "family | group | recipes", "--- | --- | ---"]
    for fam, spec in PACT_FAMILIES.items():
        L.append(f"{fam} | {spec['group']} | {by_fam.get(fam, 0)}")
    L += ["", f"{len(cases)} prior cases.", "",
          "## No equivalent", "",
          "- Families with 0 recipes: pramana only measures whole-design Vivado commands and custom "
          "Tcl, never RapidWright, pblocks or object-targeted (net/cell window) edits. Its "
          "actions are global (`targets: whole design`), not localised ECOs.",
          "- `hypotheses`, `pruning` audit, `feature_ranges`, `design_features`, `llm_spend`: not used.",
          "- PACT's within-run admissibility (budget, duplicates) is runtime state, not KB content.", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------- entry
def write_all(kb: dict, b: dict, out_dir: Path) -> dict[str, Path]:
    """Write out_dir/latch/... and out_dir/pact/...; returns {relative name: path}."""
    out_dir = Path(out_dir)
    files = {}
    files.update(write_latch(kb, b, out_dir / "latch"))
    files.update(write_pact(kb, b, out_dir / "pact"))
    return files
