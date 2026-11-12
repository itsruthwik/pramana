"""S7b (part): the strategy selector, how a new DCP is matched to a strategy.

Candidates: the policies (chains) measured on every training design (the S6 policy matrix),
up to CAND_K chosen to cover the designs (the best by mean score, then greedily the one that
most raises each design's best available score). A design's score for a policy is its gain as a fraction of the
best gain known on that design (distill._frac, -1..1).

Selectors are code with fit(train) and predict(model, reports, features, budget) (see
pramana/mithya_shim/selector.py). Two are built in (ONE_BIN: the best single policy; STUMP:
one base feature, one threshold, a policy per side); the LLM proposes up to N_LLM more, any
method (rules on report-derived quantities, principal components, nearest neighbours, ...).

Training rows are (design, budget) pairs: the budget (BUDGETS, x the design's route Explore
time) is a selector input. Each selector is scored twice, on the same per-row score:
  lofo  leave-one-family-out over the training designs (fit without the family, predict it);
  val   fit on all training designs, predict the validation designs (cfg.val_campaign), on
        which every candidate policy has been run (pramana never searches there).
lofo is in-sample for LLM selectors: the LLM saw every training design's scores when it wrote
the code, so constants it hard-codes were chosen on the held-out family too. Only val (designs
the LLM never saw) measures generalisation, which is why shipping needs a val win.
The shipped selector is the one with the best val score among those that beat ONE_BIN on both
lofo and val; otherwise ONE_BIN ships.
"""
from __future__ import annotations

import importlib.util
import json
import re
import statistics
from pathlib import Path

from . import llm, profile_ext
from .campaign import Campaign
from .config import REPO, Config

CAND_K = 8
N_LLM = 4
EXCERPT_LINES = 40
INVALID = -0.05
BUDGETS = [3, 6, 12, 25, 50, None]   # x the design's route Explore time; None: unlimited


def _shim(name: str):
    spec = importlib.util.spec_from_file_location(f"pramana_shim_{name}",
                                                  REPO / "pramana" / "mithya_shim" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


extract = _shim("extract")

ONE_BIN = '''"""The single policy with the best mean score over the training designs."""


def fit(train):
    names = sorted({p for t in train for p in t["scores"]})
    mean = {p: sum(t["scores"].get(p, -1.0) for t in train) / len(train) for p in names}
    return {"policy": max(names, key=lambda p: (mean[p], p))}


def predict(model, r, f, budget):
    return model["policy"]
'''

STUMP = '''"""One base feature and one threshold; the best policy on each side."""


def _best(rows, names):
    return max(names, key=lambda p: (sum(t["scores"].get(p, -1.0) for t in rows) / len(rows), p))


def fit(train):
    names = sorted({p for t in train for p in t["scores"]})
    def mean(rows, p):
        return sum(t["scores"].get(p, -1.0) for t in rows) / len(rows)
    best = None
    feats = sorted({k for t in train for k, v in t["features"].items() if isinstance(v, (int, float))})
    for k in feats:
        if any(t["features"].get(k) is None for t in train):
            continue
        vals = sorted({t["features"][k] for t in train})
        for a, b in zip(vals, vals[1:]):
            th = (a + b) / 2
            lo = [t for t in train if t["features"][k] <= th]
            hi = [t for t in train if t["features"][k] > th]
            if len(lo) < 2 or len(hi) < 2:
                continue
            pl, ph = _best(lo, names), _best(hi, names)
            s = (sum(mean([t], pl) for t in lo) + sum(mean([t], ph) for t in hi)) / len(train)
            if best is None or s > best[0]:
                best = (s, k, th, pl, ph)
    if best is None:
        return {"feature": None, "fallback": _best(train, names)}
    vals = [t["features"][best[1]] for t in train]
    return {"feature": best[1], "threshold": best[2], "low": best[3], "high": best[4],
            "fallback": _best(train, names), "range": [min(vals), max(vals)]}


def predict(model, r, f, budget):
    """None (abstain: the default policy, every step a candidate) when the feature is missing
    or more than 10% of its training span outside the training range."""
    v = f.get(model["feature"]) if model.get("feature") else None
    if v is None:
        return model["fallback"] if not model.get("feature") else None
    lo, hi = model.get("range") or (v, v)
    span = (hi - lo) or abs(hi) or 1.0
    if not lo - 0.1 * span <= v <= hi + 0.1 * span:
        return None
    return model["low"] if v <= model["threshold"] else model["high"]
'''

SCHEMA = {
    "type": "object", "required": ["selectors"],
    "properties": {"selectors": {"type": "array", "items": {
        "type": "object", "required": ["name", "method", "rationale", "code"],
        "properties": {"name": {"type": "string"}, "method": {"type": "string"},
                       "rationale": {"type": "string"}, "code": {"type": "string"}}}}}}


def _frac(g: float, o: float) -> float:
    if o <= 0:
        return 1.0 if g >= o else 0.0
    return max(-1.0, min(1.0, g / o))


# --------------------------------------------------------------------------- data
def candidates(M: dict, designs: list[str], oracle: dict) -> list[str]:
    """Up to CAND_K policies that together cover the designs: first the best by mean score, then
    greedily the one that most raises the mean over designs of the best score among those chosen
    (stops early when no policy raises it)."""
    full = [k for k, row in M.items() if all(d in row for d in designs)]
    S = {k: {d: _frac(M[k][d], oracle[d]) for d in designs} for k in full}
    if not full:
        return []
    out = [max(sorted(full), key=lambda k: statistics.mean(S[k].values()))]
    best = dict(S[out[0]])
    while len(out) < CAND_K:
        gain = {k: statistics.mean(max(best[d], S[k][d]) for d in designs) - statistics.mean(best.values())
                for k in full if k not in out}
        k = max(sorted(gain), key=gain.get, default=None)
        if k is None or gain[k] <= 1e-4:
            break
        out.append(k)
        best = {d: max(best[d], S[k][d]) for d in designs}
    return out


def alias(cands: list[str]) -> dict[str, str]:
    """Short policy names (P0, P1, ...) used by selectors and shipped in policy.json."""
    return {f"P{i}": k for i, k in enumerate(cands)}


def timeline(r: dict) -> list[tuple[float, float]]:
    """(cumulative step seconds, WNS) at every routed state a run passed through."""
    out, t, routed = [], 0.0, True
    for s in r.get("steps") or []:
        t += s.get("secs") or 0.0
        if not s.get("ok"):
            break
        name = s["name"]
        if "place" in name or "_opt" in name and "phys_opt" not in name:
            routed = False
        if "route" in name:
            routed = True
        if routed and s.get("wns_after") is not None:
            out.append((t, s["wns_after"]))
    return out


def timelines(c: Campaign, cands: list[str], ids: list[str]) -> dict:
    """{policy: {design: timeline}} of the valid runs of each candidate from the input."""
    tl: dict = {k: {} for k in cands}
    for r in c.results():
        if r["design"] in ids and r["start"] == "input" and r["chain_key"] in tl \
                and not r.get("extra_tcl") and r["valid"]:
            tl[r["chain_key"]][r["design"]] = timeline(r)
    return tl


def design_rows(c: Campaign, ids: list[str], table: dict, oracle: dict, cands: list[str],
                ref: dict) -> list[dict]:
    """One row per design and budget. Within a budget a policy (keep-if-better) is worth the best
    routed state it reached by then (0 if none)."""
    fam = {d["id"]: d["family"] for d in c.designs()}
    tl = timelines(c, cands, ids)
    rows = []
    for did in ids:
        d = c.design(did)
        base = {"id": did, "family": fam.get(did, ""), "features": c.features(did),
                "reports": extract.load_reports(c.profile_dir(did), profile_ext.ext_dir(c.cfg, d))}
        w0 = c.input_state(did)["wns"]
        for b in BUDGETS:
            sc = {}
            for a, k in alias(cands).items():
                if did not in table.get(k, {}):
                    continue
                if not tl[k].get(did):               # invalid run or no step log
                    g = min(table[k][did], 0.0) if b is not None else table[k][did]
                else:
                    g = max([w - w0 for t, w in tl[k][did]
                             if b is None or t <= b * ref[did]] + [0.0])
                sc[a] = round(_frac(g, oracle[did]), 4)
            rows.append({**base, "budget": b, "scores": sc})
    return rows


def _key(t: dict) -> str:
    return f"{t['id']}@{'inf' if t['budget'] is None else t['budget']}"


def validation_table(c: Campaign, pols: dict, cands: list[str], log=print):
    """Run every candidate policy on the validation designs (from their input); returns
    (val campaign, {policy: {design: d_wns}}, {design: oracle}), or None if no val campaign."""
    if not c.cfg.val_campaign:
        return None
    from .cli import stage
    vcfg = Config.load(c.cfg.val_campaign)
    vcfg.workers = c.cfg.workers
    v = Campaign(vcfg)
    if not all(r for r in (v.results(d["id"]) for d in v.designs())):
        for s in ("space", "profile", "probe", "baseline"):
            stage(s, vcfg, log)
    profile_ext.run(vcfg, v.designs(), log=log)
    jobs = [v.job(d["id"], v.input_state(d["id"]), pols[k]["chain"], "validate", tag=k[:60])
            for d in v.designs() for k in cands]
    log(f"[strategy] validation: {len(cands)} candidate policies x {len(v.designs())} designs "
        f"of {vcfg.name}")
    v.runner.run_many(jobs, log=log)
    from .runner import run_id
    by_id = {r["run_id"]: r for r in v.results()}
    table: dict = {k: {} for k in cands}
    for j, k in zip(jobs, [k for _ in v.designs() for k in cands]):
        r = by_id.get(run_id(j["design"], j["start"], j["chain"], 0, ""))
        if r:
            table[k][j["design"]] = r["d_wns"] if r["valid"] else INVALID
    oracle = {d["id"]: max([0.0] + [table[k][d["id"]] for k in cands if d["id"] in table[k]])
              for d in v.designs()}
    from .distill import _ref_secs
    return v, table, oracle, {d['id']: _ref_secs(v, d['id']) for d in v.designs()}


# --------------------------------------------------------------------------- scoring
class Tracked(dict):
    """A report dict that records which reports a selector reads."""
    def __init__(self, d, used):
        super().__init__(d)
        self.used = used

    def __getitem__(self, k):
        self.used.add(k)
        return super().__getitem__(k)

    def get(self, k, default=None):
        self.used.add(k)
        return super().get(k, default)

    def __contains__(self, k):
        self.used.add(k)
        return super().__contains__(k)

    def _all(self):                   # iterating reads every report: all are needed
        self.used.update(super().keys())

    def __iter__(self):
        self._all()
        return super().__iter__()

    def keys(self):
        self._all()
        return super().keys()

    def items(self):
        self._all()
        return super().items()

    def values(self):
        self._all()
        return super().values()


def _fit_predict(sel, path: Path, train: list[dict], test: list[dict], cands: list[str],
                 used: set | None = None) -> list:
    model = sel.fit(path, train)
    return model, [(t, sel.predict(path, model, Tracked(t["reports"], used) if used is not None
                                   else t["reports"], t["features"], t["budget"], cands))
                   for t in test]


def _score(preds: list) -> float:
    return statistics.mean(t["scores"].get(p, -1.0) if p else -1.0 for t, p in preds)


def lofo(sel, path: Path, rows: list[dict], cands: list[str]) -> float:
    fams = sorted({t["family"] for t in rows})
    preds = []
    for F in fams:
        tr = [t for t in rows if t["family"] != F]
        te = [t for t in rows if t["family"] == F]
        preds += _fit_predict(sel, path, tr, te, cands)[1]
    return round(_score(preds), 4)


# --------------------------------------------------------------------------- LLM
def _prompt(rows: list[dict], pols: dict, cands: list[str]) -> str:
    sample = rows[0]
    L = [f"Write {N_LLM} STRATEGY SELECTORS for Vivado post-route timing closure: given a "
         "new routed design's profile reports and a time budget, pick which of the candidate "
         "policies (command sequences) to run. Each selector must use a different method, e.g. thresholds on a "
         "quantity you compute from the reports, principal components, nearest training "
         "designs, a scoring rule per policy. Aim for methods that generalise to design "
         "families not seen in training (they are tested that way).", "",
         "The base features are a small fixed summary. The reports (listed at the end) hold much "
         "more: per-hierarchy utilization and timing, hold paths, clock interaction, CDC, "
         "congestion, methodology and DRC findings, power, the worst 1000 setup paths. At least "
         f"{N_LLM - 1} of your selectors must parse quantities out of the report texts r[...] "
         "and base their decision on them (alone or with features); say in the method line "
         "which reports and quantities. Read features by name (f['util_lut']), never by "
         "position in a list.", "",
         "Code: Python source defining fit(train) -> model and predict(model, r, f, budget) -> ONE "
         "policy name 'P0', 'P1', ... (or None to abstain: the default policy then runs with "
         "every step kept only if it helps). budget is the time budget in multiples of the "
         "design's route_design -directive Explore time (None: unlimited); a short budget favours "
         "policies that gain early. train is a list of rows, one per design and budget, each "
         "{'id', 'family', 'budget', 'features': {name: value}, 'reports': {name: text}, "
         "'scores': {policy: fraction of the design's best known gain reached within that "
         "budget, -1..1}} (rows of one design share its reports and features). model must be JSON-serializable (it is "
         "shipped as data). r is the new design's reports, f its base features. Standard "
         "library only, no file access; parse defensively (a report may be missing or start "
         "with 'REPORT_ERROR').", "",
         "Candidate policies:"]
    for a, k in alias(cands).items():
        L.append(f"- {a}: {k!r}")
    L += ["", "Per-design base features, then scores per budget (columns P0..; fraction of the "
          "best known gain reached within the budget, x route Explore time; inf: unlimited):"]
    for t in rows:
        if t["budget"] == BUDGETS[0]:
            L.append(f"- {t['id']} [{t['family']}]: features "
                     + json.dumps({k: v for k, v in t["features"].items() if v is not None}))
        L.append(f"    {'inf' if t['budget'] is None else t['budget']}x: " + " ".join(
            f"{a}={t['scores'].get(a, float('nan')):.2f}" for a in alias(cands)))
    L += ["", f"Reports available (key, size, first {EXCERPT_LINES} lines from {sample['id']}):"]
    for k, txt in sample["reports"].items():
        L.append(f"\n### r[{k!r}] ({len(txt)} chars)\n```\n"
                 + "\n".join(txt.splitlines()[:EXCERPT_LINES]) + "\n```")
    L += ["", "Give each selector a short snake_case name, the method in one line, and the "
          "rationale in one line (what design property decides the policy, in Vivado terms)."]
    return "\n".join(L)


# --------------------------------------------------------------------------- main
def run(c: Campaign, M: dict, oracle: dict, pols: dict, designs: list[str], ref: dict,
        log=print) -> dict:
    sel = _shim("selector")
    sd = c.root / "kb" / "selectors"
    sd.mkdir(parents=True, exist_ok=True)
    cands = candidates(M, designs, oracle)
    names = list(alias(cands))
    rows = design_rows(c, designs, M, oracle, cands, ref)
    log(f"[strategy] {len(cands)} candidate policies, {len(designs)} training designs x "
        f"{len(BUDGETS)} budgets, "
        f"{len({t['family'] for t in rows})} families")
    reg = {"one_bin": {"method": "best single policy", "code": ONE_BIN, "builtin": True},
           "stump": {"method": "one base feature, one threshold", "code": STUMP, "builtin": True}}
    out = llm.ask(c.cfg, "distill", "selectors", _prompt(rows, pols, cands), SCHEMA)
    got = (out or {}).get("selectors") or []
    log(f"[strategy] LLM returned {len(got)} selector(s) (asked for {N_LLM})"
        + ("" if out else "; no valid answer"))
    for p in got[:N_LLM]:
        name = "llm_" + re.sub(r"[^a-z0-9_]", "_", p["name"].lower()).strip("_")[:40]
        reg[name] = {"method": p["method"], "rationale": p["rationale"], "code": p["code"],
                     "builtin": False}
    val = validation_table(c, pols, cands, log)
    vrows = []
    if val:
        v, vtable, voracle, vref = val
        vrows = design_rows(v, [d["id"] for d in v.designs()], vtable, voracle, cands, vref)
    for name, s in reg.items():
        path = sd / f"{name}.py"
        path.write_text(s["code"])
        try:
            s["lofo"] = lofo(sel, path, rows, names)
            model, preds = _fit_predict(sel, path, rows, vrows, names)
            s["model"] = model
            s["val"] = round(_score(preds), 4) if vrows else None
            s["val_picks"] = {_key(t): p for t, p in preds}
            used = set()
            s["train_picks"] = {_key(t): p for t, p in
                                _fit_predict(sel, path, rows, rows, names, used)[1]}
            s["reports_used"] = sorted(used)
            s["status"] = "scored"
        except Exception as e:
            s.update(status="failed", reason=str(e)[:300], lofo=None, val=None, reports_used=[])
        log(f"[strategy] {name:32s} lofo={s.get('lofo')} val={s.get('val')} "
            f"reports={len(s['reports_used'])} {s.get('reason') or ''}"
            f"{' FAILED' if s['status'] != 'scored' else ''}")
    base = reg["one_bin"]
    ok = [n for n, s in reg.items() if n != "one_bin" and s["status"] == "scored"
          and s["lofo"] > base["lofo"] and s["val"] is not None and s["val"] > base["val"]]
    pick = max(ok, key=lambda n: (reg[n]["val"], reg[n]["lofo"])) if ok else "one_bin"
    log(f"[strategy] shipped: {pick} (lofo {reg[pick]['lofo']}, val {reg[pick]['val']}; "
        f"one_bin lofo {base['lofo']}, val {base['val']})"
        + ("" if val else " -- no validation campaign set (cfg.val_campaign): one_bin ships"))
    (sd / "selectors.json").write_text(json.dumps(
        {n: {k: v for k, v in s.items() if k != "code"} for n, s in reg.items()}, indent=1))
    return {"name": pick, "file": f"selectors/{pick}.py", "code": reg[pick]["code"],
            "model": reg[pick]["model"], "lofo": reg[pick]["lofo"], "val": reg[pick]["val"],
            "default": base["model"]["policy"], "candidates": alias(cands),
            "policies": {a: {"key": k, "chain": pols[k]["chain"], "units": pols[k].get("units")}
                         for a, k in alias(cands).items()},
            "train_picks": reg[pick]["train_picks"], "reports_used": reg[pick]["reports_used"],
            "val_campaign": c.cfg.val_campaign or None,
            "all": {n: {k: s.get(k) for k in ("method", "rationale", "builtin", "status", "reason",
                                               "lofo", "val", "val_picks", "reports_used")}
                    for n, s in reg.items()}}
