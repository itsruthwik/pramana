"""S3: LLM prunes each design's search space from reports + Explore evidence.

The LLM prunes FACTORS (commands, directives, option values), not points. Every cut carries a
reason and confidence and is a hypothesis: the audit share of later rounds runs pruned-out
points and measures whether cuts were right. Pruning is hard (pruned factors get only the
audit budget), per design, and redone every round (cuts can be reversed).
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from . import evidence, llm, session, space
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
                      "description": "What limits timing on this design, citing evidence. At most 80 words."},
        "cuts": {"type": "array", "description": "Groups of factors cut for the same specific "
                 "reason: one command per group, at most 12 factors per group.",
                 "items": {
            "type": "object",
            "properties": {"factors": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                           "reason": {"type": "string", "description": "At most 15 words."},
                           "confidence": {"type": "number"}},
            "required": ["factors", "reason", "confidence"]}},
        "uncuts": {"type": "array", "items": {"type": "string"},
                   "description": "Previously cut factors to bring back (re-prune only)."},
        "proposals": {"type": "array", "items": {
            "type": "object",
            "properties": {"chain": {"type": "array", "items": STEP_SCHEMA},
                           "predicted_d_wns": {"type": "number"},
                           "reason": {"type": "string", "description": "At most 15 words."}},
            "required": ["chain", "predicted_d_wns", "reason"]}},
        "hypotheses": {"type": "array", "items": {
            "type": "object",
            "properties": {"claim": {"type": "string", "description": "At most 25 words."},
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

- Output `cuts`: factors to REMOVE from this design's search, in groups that share one
  specific reason (e.g. the opt directives because opt passes changed 0 cells). A group holds
  factors of ONE command and at most 12 factors, and its reason must hold for every factor in
  it; a reviewer rejects the whole group if the reason is wrong for any member. Everything not
  cut stays.
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
- Be terse. Each reason is at most 15 words and names its evidence (e.g. "opt_explore log:
  0 cells changed") instead of copying it. Diagnosis at most 80 words. Do not restate the
  evidence or explain the task back; spend your words on judgement.
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


CRITIC_SCHEMA = {
    "type": "object",
    "properties": {"unsupported": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"},
                       "factors": {"type": "array", "items": {"type": "string"},
                                   "description": "For a cuts item: only the factors whose "
                                   "cut is unsupported. Omit to reject the whole group."},
                       "why": {"type": "string", "description": "At most 20 words."}},
        "required": ["id", "why"]}}},
    "required": ["unsupported"],
}

CRITIC_INSTRUCTIONS = """
## Re-check: review the analysis as a strict critic

Switch roles. The evidence for this design is already in this conversation; do not re-read
it in full, look things up only to check a specific claim. Below are the items of the
analysis{merged}. For each, check only whether its reason holds against that evidence:
- Unsupported: the reason CONTRADICTS or INVENTS evidence (e.g. says a pass optimised
  nothing when its table shows it did, or cites a number that is not there).
- Supported: it follows from the evidence, is a reasonable reading of it, or is a standard
  property of Vivado. Evidence that is merely incomplete (a report that does not mention
  something) is not a contradiction; do not reject for that.
Do not judge whether the choice is wise or whether you would phrase it better, only whether
its reason is contradicted or invented.
List ONLY unsupported items (id + why, at most 20 words). For a cut group whose reason holds
for some factors but not others, give `factors`: only the ones it does not hold for. An empty
list is valid.

{items}
"""

# Resumed sessions already hold the design evidence from their first turn; later rounds send
# only what is new, so the conversation does not carry a copy of the evidence per round.
RESUME_NOTE = ("The design evidence (reports, Explore baseline runs, knowledge base, space and "
               "factors) is in your first message in this conversation and has not changed. "
               "Below is only what is new since your last answer.")


def resumable(c: Campaign, key: str) -> bool:
    """True if `key` maps to a session whose transcript exists, i.e. llm.ask will resume it."""
    sid = session.SessionStore(c.cfg).get(key)
    return sid is not None and session.transcript(sid) is not None


def _expand(out: dict) -> dict:
    """Grouped cuts ({factors, reason, confidence}) -> one cut per factor, the form the rest
    of pramana (merge, validate, search audits, distill) uses."""
    cuts = []
    for g in out.get("cuts", []):
        for f in g.get("factors") or ([g["factor"]] if "factor" in g else []):
            cuts.append({"factor": f, "reason": g["reason"], "confidence": g["confidence"]})
    return {**out, "cuts": cuts}


def _merge(outs: list[dict]) -> dict:
    """Several analysts' answers -> one. A factor is cut if more than half the analysts cut it
    (at exactly half: if their mean confidence is >= 0.7). Proposals and hypotheses are
    pooled; identical chains are merged with the mean prediction."""
    if len(outs) == 1:
        return outs[0]
    n = len(outs)
    votes: dict[str, list[dict]] = {}
    for o in outs:
        for ct in o.get("cuts", []):
            votes.setdefault(ct["factor"], []).append(ct)
    cuts = []
    for f, vs in votes.items():
        conf = sum(v["confidence"] for v in vs) / len(vs)
        if len(vs) * 2 > n or (len(vs) * 2 == n and conf >= 0.7):
            cuts.append({"factor": f, "confidence": round(conf, 3),
                         "reason": " | ".join(v["reason"] for v in vs), "votes": len(vs)})
    props: dict[str, dict] = {}
    for i, o in enumerate(outs):
        for p in o.get("proposals", []):
            k = space.chain_key([clean_step(s) for s in p["chain"]])
            if k in props:
                q = props[k]
                q["_preds"].append(p["predicted_d_wns"])
                q["reason"] += " | " + p["reason"]
            else:
                props[k] = {**p, "_preds": [p["predicted_d_wns"]], "analyst": i}
    for q in props.values():
        q["predicted_d_wns"] = round(sum(q["_preds"]) / len(q["_preds"]), 4)
        q["n_analysts"] = len(q.pop("_preds"))
    uncuts = sorted({u for o in outs for u in o.get("uncuts", [])})
    return {"diagnosis": "\n\n".join(f"[analyst {i}] {o.get('diagnosis', '')}"
                                      for i, o in enumerate(outs)),
            "cuts": cuts, "uncuts": uncuts, "proposals": list(props.values()),
            "hypotheses": [h for o in outs for h in o.get("hypotheses", [])]}


def _critic_items(out: dict) -> tuple[str, dict]:
    lines, ids = [], {}
    groups: dict[str, list[dict]] = {}
    for ct in out.get("cuts", []):
        groups.setdefault(ct["reason"], []).append(ct)
    for j, (reason, cts) in enumerate(groups.items()):
        i = f"cuts:{j}"
        ids[i] = cts
        conf = round(sum(x["confidence"] for x in cts) / len(cts), 2)
        lines.append(f"- id `{i}`: CUT {', '.join(x['factor'] for x in cts)} "
                     f"(confidence {conf}). Reason: {reason}")
    for j, p in enumerate(out.get("proposals", [])):
        i = f"prop:{j}"
        ids[i] = p
        lines.append(f"- id `{i}`: PROPOSE {space.chain_key([clean_step(x) for x in p['chain']])} "
                     f"(predicted dWNS {p['predicted_d_wns']}). Reason: {p['reason']}")
    for j, h in enumerate(out.get("hypotheses", [])):
        i = f"hyp:{j}"
        ids[i] = h
        lines.append(f"- id `{i}`: HYPOTHESIS {h['claim']} (prediction: {h['prediction']})")
    return "\n".join(lines), ids


def critique(c: Campaign, did: str, rnd: int, out: dict, n: int, key: str) -> dict:
    """A second turn in the analyst's own session (`key`), which already holds the evidence,
    so the prompt is just the review rules and the items. Drop cuts and proposals whose
    reasons it finds unsupported (the factor stays in
    the search; the chain is not run); hypotheses are kept but marked. If the critic fails,
    the answer passes unchanged."""
    items, ids = _critic_items(out)
    if not ids:
        return {**out, "critic": {"status": "nothing to check"}}
    merged = f" (merged from {n} analysts)" if n > 1 else ""
    v = llm.ask(c.cfg, "critic", f"{did}_r{rnd}_critic",
                CRITIC_INSTRUCTIONS.format(merged=merged, items=items), CRITIC_SCHEMA,
                system=SYSTEM, key=key)   # same system prompt as the analyst: keeps the cache
    if v is None:
        return {**out, "critic": {"status": "failed; answer not checked"}}
    bad = {x["id"]: x["why"] for x in v["unsupported"] if x["id"] in ids}
    dropped = set()
    for x in v["unsupported"]:
        if x["id"] in ids and x["id"].startswith("cuts:"):
            members = {ct["factor"] for ct in ids[x["id"]]}
            only = set(x.get("factors") or []) & members
            dropped |= only or members      # named factors only, else the whole group
    hyps = [{**h, "critic": "unsupported: " + bad[f"hyp:{j}"]} if f"hyp:{j}" in bad else h
            for j, h in enumerate(out.get("hypotheses", []))]
    return {**out,
            "cuts": [ct for ct in out.get("cuts", []) if ct["factor"] not in dropped],
            "proposals": [p for j, p in enumerate(out.get("proposals", [])) if f"prop:{j}" not in bad],
            "hypotheses": hyps,
            "critic": {"status": "ok", "checked": len(ids), "rejected": bad}}


def prune_design(c: Campaign, did: str, rnd: int, start: dict, extra: str = "",
                 prev: dict | None = None) -> dict:
    spec = c.spec()
    n_total = len(space.all_factors(spec))
    lineage = " > ".join(c.lineage(did, start["id"])) or "(input DCP, unmodified)"
    evid = "\n\n".join([
        evidence.design_brief(c, did),
        evidence.baseline_brief(c, did),
        evidence.kb_brief(c, did),
        extra])
    start_md = f"## Start state for proposals\nstate {start['id']}: WNS {start['wns']} ns, reached by: {lineage}"
    task_md = INSTRUCTIONS.format(n_total=n_total, n_keep=int(n_total * KEEP_FRACTION),
                                  n_cut=n_total - int(n_total * KEEP_FRACTION))
    full = "\n\n".join([evid, evidence.space_brief(spec),
                         "## Factors\n" + factors_by_command(spec), start_md, task_md])
    # a resumed analyst already has the evidence; it gets the new results and the task again
    delta = "\n\n".join([RESUME_NOTE, extra, start_md, task_md])
    models = c.cfg.analysts or [c.cfg.llm_model]

    def one(i_m):
        i, m = i_m
        # the first analyst keeps the historical session key, so old campaigns resume it
        key = f"analyst_{did}" if i == 0 else f"analyst{i}_{did}"
        name = f"{did}_r{rnd}" if i == 0 else f"{did}_r{rnd}_a{i}"
        prompt = delta if extra and resumable(c, key) else full
        return key, llm.ask(c.cfg, "prune", name, prompt, SCHEMA, system=SYSTEM, key=key, model=m)
    with ThreadPoolExecutor(len(models)) as ex:
        answered = [(k, _expand(o)) for k, o in ex.map(one, enumerate(models)) if o is not None]
    outs = [o for _, o in answered]
    out = _merge(outs) if outs else None
    if out is not None and c.cfg.critic:
        out = critique(c, did, rnd, out, len(outs), key=answered[0][0])
    if out is None:
        # LLM failure: keep the previous prune, or keep everything
        res = prev or validate(spec, {"cuts": [], "proposals": [], "hypotheses": []})
        res = {**res, "llm_failed": True}
    else:
        res = validate(spec, out)
        res["n_analysts"] = len(outs)
        if "critic" in out:
            res["critic"] = out["critic"]
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
