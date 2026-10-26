"""S0 capability probe: which directives/values actually run on this part.

Vivado's help lists some Advanced-Flow-only choices, and UG904's legacy UltraScale+ placer
directives aren't in help at all. Each uncertain choice is run once on the smallest design;
a choice that errors is removed from the spec. Valid probe runs are normal results (reused by
the search through the case record).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import space
from .campaign import Campaign


def probe_steps(spec: dict) -> list[dict]:
    steps = []
    pl = spec["commands"]["place"]
    for d, meta in pl["directives"].items():
        if meta.get("probe_required") or d == "AggressiveExplore":
            steps.append({"cmd": "place", "directive": d})
    sub = pl["options"].get("-subdirective")
    if sub:
        seen = set()
        for v in sub["values"]:
            name = ".".join(v.split(".")[:2])
            if name not in seen:
                seen.add(name)
                steps.append({"cmd": "place", "opts": {"-subdirective": [v]}})
    psip = pl["options"].get("-psip_options")
    if psip:
        steps.append({"cmd": "place", "opts": {"-psip_options": [psip["values"][0]]}})
    if "-net_delay_weight" in pl["options"]:
        steps.append({"cmd": "place", "opts": {"-net_delay_weight": "high"}})
    return steps


def run(c: Campaign, log=print) -> dict:
    spec = c.spec()
    if spec.get("probed"):
        log("probe: already applied")
        return {}
    d = min(c.designs(), key=lambda x: x["dcp_mb"])
    st = c.input_state(d["id"])
    steps = probe_steps(spec)
    jobs = [c.job(d["id"], st, [s], "probe") for s in steps]
    c.runner.run_many(jobs, log=log)
    by_key = {r["chain_key"]: r for r in c.results(d["id"]) if r["purpose"] == "probe"}
    probe: dict = {}
    for s in steps:
        r = by_key.get(space.chain_key([s]))
        if r is None:
            continue
        err = next((x["error"] for x in r["steps"] if not x["ok"]), None)
        if r["status"] == "ok":
            continue
        entry = probe.setdefault(s["cmd"], {"invalid_directives": [], "invalid_values": {},
                                            "invalid_options": [], "errors": {}})
        if s.get("directive"):
            entry["invalid_directives"].append(s["directive"])
        for k, v in (s.get("opts") or {}).items():
            if k == "-subdirective":
                name = ".".join(v[0].split(".")[:2])
                bad = [x for x in spec["commands"]["place"]["options"][k]["values"]
                       if x.startswith(name + ".") or x == name]
                entry["invalid_values"].setdefault(k, []).extend(bad)
            else:
                entry["invalid_options"].append(k)
        entry["errors"][space.step_key(s)] = (err or r["status"])[:300]
    (c.root / "probe.json").write_text(json.dumps(probe, indent=1))
    spec = space.apply_probe(spec, probe)
    space.save(spec, c.root / "space.json")
    log(f"probe: {sum(len(v['errors']) for v in probe.values())} choices invalid; "
        f"space now {json.dumps(spec['size'])}")
    run_postroute(c, log=log)
    return probe


def run_postroute(c: Campaign, log=print) -> dict:
    """Which phys_opt options Vivado accepts on a ROUTED design (unsupported ones fail in <1 s)."""
    spec = c.spec()
    d = min(c.designs(), key=lambda x: x["dcp_mb"])
    st = c.input_state(d["id"])
    opts = [k for k, o in spec["commands"]["phys_opt"]["options"].items() if o["type"] == "switch"]
    jobs = [c.job(d["id"], st, [{"cmd": "phys_opt", "opts": {k: True}}], "probe") for k in opts]
    c.runner.run_many(jobs, log=log)
    by_key = {r["chain_key"]: r for r in c.results(d["id"])}
    bad = {}
    for k in opts:
        r = by_key.get(space.chain_key([{"cmd": "phys_opt", "opts": {k: True}}]))
        if r is None:
            continue
        err = next((x["error"] for x in r["steps"] if not x["ok"]), None)
        ok = r["status"] == "ok"
        if not ok and err:
            # the real reason is in the log line before 'failed due to earlier errors'
            log_txt = (Path(r["run_dir"]) / "vivado.log").read_text(errors="replace")
            m = re.search(r"^ERROR: \[Vivado_Tcl 4-265\].*$", log_txt, re.M)
            if m:
                bad[k] = m.group(0)[:200]
        spec["commands"]["phys_opt"]["options"][k]["postroute"] = k not in bad
    spec["postroute_probed"] = True
    space.save(spec, c.root / "space.json")
    (c.root / "probe_postroute.json").write_text(json.dumps(bad, indent=1))
    log(f"probe-postroute: {len(bad)} phys_opt options unsupported after routing: {sorted(bad)}")
    return bad
