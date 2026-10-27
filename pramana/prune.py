"""S3: LLM prunes each design's search space from reports + Explore evidence.

The LLM prunes FACTORS (commands, directives, option values), not points. Every cut carries a
reason and confidence and is a hypothesis: the audit share of later rounds runs pruned-out
points and measures whether cuts were right. Pruning is hard (pruned factors get only the
audit budget), per design, and redone every round (cuts can be reversed).
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from . import evidence, llm, space
from .campaign import Campaign

STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "cmd": {"type": "string", "enum": ["opt", "place", "phys_opt", "route"]},
        "directive": {"type": ["string", "null"]},
        "opts": {"type": "object"},
    },
    "required": ["cmd"],
}

SCHEMA = {
    "type": "object",
    "properties": {
        "diagnosis": {"type": "string",
                      "description": "What limits timing on this design, citing evidence."},
        "cuts": {"type": "array", "items": {
            "type": "object",
            "properties": {"factor": {"type": "string"}, "reason": {"type": "string"},
                           "confidence": {"type": "number"}},
            "required": ["factor", "reason", "confidence"]}},
        "uncuts": {"type": "array", "items": {"type": "string"},
                   "description": "Previously cut factors to bring back (re-prune only)."},
        "proposals": {"type": "array", "items": {
            "type": "object",
            "properties": {"chain": {"type": "array", "items": STEP_SCHEMA},
                           "predicted_d_wns": {"type": "number"},
                           "reason": {"type": "string"}},
            "required": ["chain", "predicted_d_wns", "reason"]}},
        "hypotheses": {"type": "array", "items": {
            "type": "object",
            "properties": {"claim": {"type": "string"},
                           "factors": {"type": "array", "items": {"type": "string"}},
                           "prediction": {"type": "string"},
                           "test_chain": {"type": "array", "items": STEP_SCHEMA}},
            "required": ["claim", "factors", "prediction"]}},
    },
    "required": ["diagnosis", "cuts", "proposals", "hypotheses"],
}

KEEP_FRACTION = 0.3

SYSTEM = ("You are an expert in AMD Vivado post-route timing closure on UltraScale+. You reason "
          "from the evidence given, cite it, and never invent measurements.")

INSTRUCTIONS = """
## Your task: prune the search space for this design

You are given a routed design's reports, its Explore baseline runs (with per-pass tables that
show what every internal optimisation did on THIS design), and the full space of Vivado
choices as FACTORS. Decide which factors are worth searching on this design.

- Output `cuts`: factors to REMOVE from this design's search. Everything not cut stays.
  This is a HARD prune: cut factors get only a small random audit budget later, and audits
  will measure whether your cuts were right. Cut aggressively where the evidence says a
  factor has nothing to act on (e.g. a pass that optimised 0 cells under Explore, no DSP/BRAM
  on critical paths, runtime-only or quality-reducing choices like Quick/RuntimeOptimized/
  -no_timing_driven), keep where there's a plausible mechanism.
- BUDGET: there are {n_total} factors; keep AT MOST {n_keep} (cut at least {n_cut}). The
  search can only afford to explore a small subspace, so rank by expected value on THIS
  design and cut the rest; low-value-but-plausible factors should be cut (audits will catch
  real mistakes). For option values with levels (low/med/high), keep only the levels you
  would actually try.
- Cutting a whole command: use the bare command factor (e.g. "opt").
- Use factor names EXACTLY as listed in "Factors".
- `confidence` in [0,1] = probability the cut is right (the factor would not give the best
  result on this design).
- `proposals`: 6-12 concrete chains to run next FROM THE START STATE given below, each with a
  predicted dWNS in ns (final WNS minus start WNS; positive = better) and a reason. Use only
  kept factors. Chains of 1-3 steps. Predictions are scored against measurements.
- `hypotheses`: 2-5 testable claims about this design (e.g. "placement, not routing, limits
  this design: re-place directives beat all route directives"), naming the factors involved,
  what result would confirm them, and optionally a test chain.
"""


def factors_by_command(spec: dict) -> str:
    out = []
    fs = space.all_factors(spec)
    for short in spec["commands"]:
        mine = [f for f in fs if f == short or f.startswith(short + ":")]
        out.append(f"{short}: " + ", ".join(mine))
    return "\n".join(out)


def allowed_from_cuts(spec: dict, cuts: set[str]) -> dict:
    """Pruned subspace in the form space.random_step() takes."""
    allowed = {}
    for short, c in spec["commands"].items():
        if short in cuts:
            continue
        dirs = [d for d in c["directives"] if f"{short}:directive={d}" not in cuts]
        opts = {}
        for k, o in c["options"].items():
            if o["type"] == "switch":
                if f"{short}:{k}" not in cuts:
                    opts[k] = True
            else:
                vals = [v for v in o["values"] if f"{short}:{k}={v}" not in cuts]
                if vals:
                    opts[k] = vals
        if dirs or opts:
            allowed[short] = {"directives": dirs, "options": opts}
    return allowed


def in_allowed(step: dict, allowed: dict) -> bool:
    a = allowed.get(step["cmd"])
    if a is None:
        return False
    if step.get("directive") and step["directive"] not in a["directives"]:
        return False
    for k, v in (step.get("opts") or {}).items():
        if k not in a["options"]:
            return False
        av = a["options"][k]
        if av is True:
            continue
        vs = v if isinstance(v, list) else [v]
        if any(x not in av for x in vs):
            return False
    return True


def clean_step(step: dict) -> dict:
    s = {"cmd": step["cmd"]}
    if step.get("directive"):
        s["directive"] = step["directive"]
    if step.get("opts"):
        s["opts"] = step["opts"]
    return s


def validate(spec: dict, out: dict, prev_cuts: set[str] | None = None) -> dict:
    valid_f = set(space.all_factors(spec))
    cuts = {}
    unknown = []
    for c in out.get("cuts", []):
        if c["factor"] in valid_f:
            cuts[c["factor"]] = c
        else:
            unknown.append(c["factor"])
    if prev_cuts:
        for f in prev_cuts:
            if f not in cuts and f not in set(out.get("uncuts", [])):
                cuts.setdefault(f, {"factor": f, "reason": "carried from previous prune",
                                    "confidence": 0.5})
    allowed = allowed_from_cuts(spec, set(cuts))
    props, bad = [], []
    for p in out.get("proposals", []):
        chain = [clean_step(s) for s in p["chain"]]
        if chain and all(space.legal(s, spec) for s in chain) and space.chain_ok(chain, spec):
            props.append({**p, "chain": chain, "in_allowed": all(in_allowed(s, allowed) for s in chain)})
        else:
            bad.append(p)
    hyps = []
    for h in out.get("hypotheses", []):
        tc = [clean_step(s) for s in h.get("test_chain") or []]
        h = {**h, "test_chain": tc if tc and all(space.legal(s, spec) for s in tc) else []}
        hyps.append(h)
    n_all = len(valid_f)
    kept = n_all - len(cuts)
    return {"diagnosis": out.get("diagnosis", ""), "cuts": list(cuts.values()),
            "unknown_factors": unknown, "allowed": allowed, "proposals": props,
            "rejected_proposals": bad, "hypotheses": hyps,
            "kept_factors": kept, "total_factors": n_all}


def prune_design(c: Campaign, did: str, rnd: int, start: dict, extra: str = "",
                 prev: dict | None = None) -> dict:
    spec = c.spec()
    n_total = len(space.all_factors(spec))
    lineage = " > ".join(c.lineage(did, start["id"])) or "(input DCP, unmodified)"
    prompt = "\n\n".join([
        evidence.design_brief(c, did),
        evidence.baseline_brief(c, did),
        evidence.kb_brief(c, did),
        extra,
        evidence.space_brief(spec),
        "## Factors\n" + factors_by_command(spec),
        f"## Start state for proposals\nstate {start['id']}: WNS {start['wns']} ns, reached by: {lineage}",
        INSTRUCTIONS.format(n_total=n_total, n_keep=int(n_total * KEEP_FRACTION),
                            n_cut=n_total - int(n_total * KEEP_FRACTION)),
    ])
    out = llm.ask(c.cfg, "prune", f"{did}_r{rnd}", prompt, SCHEMA, system=SYSTEM,
                  key=f"analyst_{did}")
    if out is None:
        # LLM failure: keep the previous prune, or keep everything
        res = prev or validate(spec, {"cuts": [], "proposals": [], "hypotheses": []})
        res = {**res, "llm_failed": True}
    else:
        res = validate(spec, out)
    res.update(round=rnd, design=did, start=start["id"], start_wns=start["wns"])
    d = c.root / "prune" / did
    d.mkdir(parents=True, exist_ok=True)
    (d / f"r{rnd}.json").write_text(json.dumps(res, indent=1))
    return res


def latest(c: Campaign, did: str) -> dict | None:
    d = c.root / "prune" / did
    files = sorted(d.glob("r*.json"), key=lambda p: int(p.stem[1:])) if d.exists() else []
    return json.loads(files[-1].read_text()) if files else None


def initial(c: Campaign, log=print) -> None:
    todo = [d["id"] for d in c.designs() if not (c.root / "prune" / d["id"] / "r0.json").exists()]

    def one(did):
        res = prune_design(c, did, 0, c.best_state(did))
        log(f"[prune] {did}: kept {res['kept_factors']}/{res['total_factors']} factors, "
            f"{len(res['proposals'])} proposals, {len(res['hypotheses'])} hypotheses"
            + (" (LLM FAILED)" if res.get("llm_failed") else ""))
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(one, todo))
