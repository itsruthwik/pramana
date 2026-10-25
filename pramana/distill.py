"""S7: distill the knowledge base from the results database.

Code computes every number. A rule enters the KB only if it holds on design families it was
not fitted on (leave-one-family-out, LOFO) and beats shuffled labels. The LLM writes
mechanism notes only; a code check rejects notes that contain numbers not in their entry's
statistics.
"""
from __future__ import annotations

import json
import math
import random
import re
import statistics
import time
from collections import defaultdict

from . import llm
from .campaign import Campaign
from .features import FEATURE_DOC, rule_features
from .logparse import pass_effects
from .space import step_factors
from pathlib import Path

INVALID = -0.05


# --------------------------------------------------------------------------- helpers
def _families(c: Campaign) -> dict[str, str]:
    return {d["id"]: d["family"] for d in c.designs()}


def _ref_secs(c: Campaign, did: str) -> float:
    for r in c.results(did):
        if r["purpose"] == "baseline" and r.get("tag") == "route_explore" and r["opt_secs"]:
            return r["opt_secs"]
    return 60.0


def _oracle(c: Campaign, did: str) -> float:
    b = c.best_state(did)
    return round(b["wns"] - c.input_state(did)["wns"], 4)


def _grade(n_designs: int, n_fams: int) -> str:
    if n_designs >= 5 and n_fams >= 3:
        return "A"
    if n_designs >= 3:
        return "B"
    return "C"


# --------------------------------------------------------------------------- actions
def action_profiles(c: Campaign) -> list[dict]:
    fam = _families(c)
    ids = set(fam)
    noise = {d: c.noise_floor(d) for d in ids}
    ref = {d: _ref_secs(c, d) for d in ids}
    per: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    incr: dict[str, list[float]] = defaultdict(list)
    for r in c.results():
        if r["design"] not in ids or r.get("extra_tcl") or not r["chain"]:
            continue
        fs = set()
        for st in r["chain"]:
            fs.update(step_factors(st))
        if r["start"] == "input" and len(r["chain"]) == 1:
            for f in fs:
                per[f][r["design"]].append(r)
        elif r["start"] != "input" and len(r["chain"]) == 1 and r["valid"]:
            for f in fs:
                incr[f].append(r["d_wns"])
    out = []
    for f, by_d in per.items():
        if ":" not in f:   # bare command = a group of actions, not an action
            continue
        eff, valid_n, all_n, costs = {}, 0, 0, []
        for did, rs in by_d.items():
            all_n += len(rs)
            v = [x for x in rs if x["valid"]]
            valid_n += len(v)
            eff[did] = max((x["d_wns"] for x in v), default=INVALID)
            costs += [x["opt_secs"] / ref[did] for x in rs if x["opt_secs"]]
        vals = list(eff.values())
        gains = sum(1 for d, e in eff.items() if e > noise[d])
        regs = sum(1 for d, e in eff.items() if e < -noise[d])
        nd, nf = len(eff), len({fam[d] for d in eff})
        prof = {
            "n_designs": nd, "n_families": nf, "n_runs": all_n,
            "median_d_wns": round(statistics.median(vals), 4),
            "mean_d_wns": round(statistics.mean(vals), 4),
            "p_gain": round(gains / nd, 3), "p_regress": round(regs / nd, 3),
            "validity": round(valid_n / all_n, 3) if all_n else None,
            "rel_cost": round(statistics.median(costs), 2) if costs else None,
            "incremental_median_d_wns": round(statistics.median(incr[f]), 4) if incr.get(f) else None,
            "incremental_n": len(incr.get(f, [])),
        }
        ship = "plain" if nd >= 3 and regs == 0 and prof["validity"] == 1.0 else "keep_if_better"
        out.append({"factor": f, "profile": prof, "per_design": {d: round(e, 4) for d, e in eff.items()},
                    "ship_mode": ship, "grade": _grade(nd, nf),
                    "evidence": sorted({x["run_id"] for rs in by_d.values() for x in rs})[:40]})
    out.sort(key=lambda a: -a["profile"]["median_d_wns"])
    return out


# --------------------------------------------------------------------------- stumps
def _stump_fit(xs: list[float], ys: list[float]) -> tuple[float, int, float] | None:
    """Best threshold t and side s (y=1 iff s*(x-t)>0) by accuracy."""
    pts = sorted(set(xs))
    if len(pts) < 2:
        return None
    best = None
    for a, b in zip(pts, pts[1:]):
        t = (a + b) / 2
        for s in (1, -1):
            acc = sum(1 for x, y in zip(xs, ys) if (s * (x - t) > 0) == bool(y)) / len(xs)
            if best is None or acc > best[2]:
                best = (t, s, acc)
    return best


def _lofo_stump(designs, feats, labels, fam, fname) -> float | None:
    """LOFO accuracy of a 1-feature stump predicting labels."""
    fams = sorted({fam[d] for d in designs})
    if len(fams) < 2:
        return None
    correct = n = 0
    for F in fams:
        tr = [d for d in designs if fam[d] != F and feats[d].get(fname) is not None]
        te = [d for d in designs if fam[d] == F and feats[d].get(fname) is not None]
        if len(tr) < 2 or not te:
            continue
        fit = _stump_fit([feats[d][fname] for d in tr], [labels[d] for d in tr])
        if fit is None:
            continue
        t, s, _ = fit
        for d in te:
            correct += (s * (feats[d][fname] - t) > 0) == bool(labels[d])
            n += 1
    return correct / n if n else None


def _lofo_majority(designs, labels, fam) -> float | None:
    fams = sorted({fam[d] for d in designs})
    correct = n = 0
    for F in fams:
        tr = [labels[d] for d in designs if fam[d] != F]
        te = [labels[d] for d in designs if fam[d] == F]
        if not tr or not te:
            continue
        maj = 1 if sum(tr) * 2 > len(tr) else 0
        correct += sum(1 for y in te if y == maj)
        n += len(te)
    return correct / n if n else None


def conditions(c: Campaign, actions: list[dict], rng: random.Random) -> list[dict]:
    fam = _families(c)
    feats = {d: rule_features(c.features(d)) for d in fam}
    fnames = sorted({k for f in feats.values() for k, v in f.items() if isinstance(v, (int, float))})
    noise = {d: c.noise_floor(d) for d in fam}
    out = []
    for a in actions:
        pdv = a["per_design"]
        designs = [d for d in pdv if d in feats]
        if len(designs) < 4:
            continue
        labels = {d: 1 if pdv[d] > noise[d] else 0 for d in designs}
        if len(set(labels.values())) < 2:
            continue
        base = _lofo_majority(designs, labels, fam)
        best = None
        for fn in fnames:
            acc = _lofo_stump(designs, feats, labels, fam, fn)
            if acc is not None and (best is None or acc > best[1]):
                best = (fn, acc)
        if best is None or base is None or best[1] <= base:
            out.append({"factor": a["factor"], "status": "no reliable condition",
                        "lofo_majority_acc": base, "best_feature": best and best[0],
                        "best_lofo_acc": best and round(best[1], 3), "n_designs": len(designs)})
            continue
        # permutation null: shuffle labels, take the best feature's LOFO accuracy
        null = []
        for _ in range(100):
            vals = list(labels.values())
            rng.shuffle(vals)
            sh = dict(zip(labels, vals))
            accs = [_lofo_stump(designs, feats, sh, fam, fn) or 0 for fn in fnames]
            null.append(max(accs))
        p = (1 + sum(1 for x in null if x >= best[1])) / (1 + len(null))
        fit = _stump_fit([feats[d][best[0]] for d in designs], [labels[d] for d in designs])
        t, s, _ = fit
        rule = f"{best[0]} {'>' if s > 0 else '<='} {round(t, 4)}"
        out.append({"factor": a["factor"], "status": "condition" if p < 0.2 else "weak (fails shuffle test)",
                    "rule": f"helps when {rule}", "feature": best[0], "threshold": round(t, 4),
                    "side": s, "lofo_acc": round(best[1], 3), "lofo_majority_acc": round(base, 3),
                    "perm_p": round(p, 3), "n_designs": len(designs),
                    "grade": _grade(len(designs), len({fam[d] for d in designs}))})
    return out


# --------------------------------------------------------------------------- policies
def policy_matrix(c: Campaign) -> tuple[dict, dict]:
    """policy chain_key -> {design: d_wns from input}; also chain per policy."""
    fam = _families(c)
    pols_p = c.root / "policies.json"
    pols = json.loads(pols_p.read_text()) if pols_p.exists() else {}
    M: dict[str, dict[str, float]] = defaultdict(dict)
    for r in c.results():
        if r["design"] in fam and r["start"] == "input" and r["chain_key"] in pols \
                and not r.get("extra_tcl"):
            v = r["d_wns"] if r["valid"] else INVALID
            M[r["chain_key"]][r["design"]] = max(v, M[r["chain_key"]].get(r["design"], -9))
    return dict(M), pols


def _pick(M, designs, oracle) -> str | None:
    best, bs = None, None
    for k, row in M.items():
        if not all(d in row for d in designs):
            continue
        s = statistics.mean(_frac(row[d], oracle[d]) for d in designs)
        if bs is None or s > bs:
            best, bs = k, s
    return best


def _frac(g, o):
    if o <= 0:
        return 1.0 if g >= o else 0.0
    return max(-1.0, min(1.0, g / o))


def policies(c: Campaign, rng: random.Random) -> dict:
    fam = _families(c)
    M, pols = policy_matrix(c)
    designs = [d for d in fam if all(d in row for row in M.values())] if M else []
    if len(designs) < 3 or not M:
        return {"status": "not enough transfer data", "n_policies": len(M)}
    oracle = {d: _oracle(c, d) for d in designs}
    feats = {d: rule_features(c.features(d)) for d in designs}
    fnames = sorted({k for f in feats.values() for k, v in f.items() if isinstance(v, (int, float))})
    one = _pick(M, designs, oracle)

    def lofo_one(ds):
        fams = sorted({fam[d] for d in ds})
        scores = []
        for F in fams:
            tr = [d for d in ds if fam[d] != F]
            te = [d for d in ds if fam[d] == F]
            k = _pick(M, tr, oracle)
            scores += [_frac(M[k][d], oracle[d]) for d in te]
        return statistics.mean(scores)

    def fit_two(ds, fn, fv):
        pts = sorted({fv[d][fn] for d in ds if fv[d].get(fn) is not None})
        best = None
        for a, b in zip(pts, pts[1:]):
            t = (a + b) / 2
            lo = [d for d in ds if fv[d][fn] <= t]
            hi = [d for d in ds if fv[d][fn] > t]
            if len(lo) < 2 or len(hi) < 2:
                continue
            kl, kh = _pick(M, lo, oracle), _pick(M, hi, oracle)
            s = statistics.mean([_frac(M[kl][d], oracle[d]) for d in lo] +
                                [_frac(M[kh][d], oracle[d]) for d in hi])
            if best is None or s > best[0]:
                best = (s, t, kl, kh)
        return best

    def lofo_two(ds, fn, fv):
        fams = sorted({fam[d] for d in ds})
        scores = []
        for F in fams:
            tr = [d for d in ds if fam[d] != F]
            te = [d for d in ds if fam[d] == F]
            fit = fit_two(tr, fn, fv)
            if fit is None:
                k = _pick(M, tr, oracle)
                scores += [_frac(M[k][d], oracle[d]) for d in te]
                continue
            _, t, kl, kh = fit
            scores += [_frac(M[kl if fv[d][fn] <= t else kh][d], oracle[d]) for d in te]
        return statistics.mean(scores)

    one_lofo = lofo_one(designs)
    two = []
    for fn in fnames:
        if any(feats[d].get(fn) is None for d in designs):
            continue
        two.append((lofo_two(designs, fn, feats), fn))
    two.sort(reverse=True)
    result = {
        "designs": designs,
        "oracle_d_wns": oracle,
        "matrix": {k: {d: round(v, 4) for d, v in row.items()} for k, row in M.items()},
        "one_bin": {"policy": one, "chain": pols.get(one, {}).get("chain"),
                    "units": pols.get(one, {}).get("units"),
                    "train_frac_of_oracle": round(statistics.mean(_frac(M[one][d], oracle[d]) for d in designs), 3),
                    "lofo_frac_of_oracle": round(one_lofo, 3)},
    }
    if two and two[0][0] > one_lofo + 0.02:
        best_s, fn = two[0]
        null = []
        for _ in range(60):
            vals = [feats[d][fn] for d in designs]
            rng.shuffle(vals)
            fv = {d: {fn: v} for d, v in zip(designs, vals)}
            null.append(lofo_two(designs, fn, fv))
        p = (1 + sum(1 for x in null if x >= best_s)) / (1 + len(null))
        fit = fit_two(designs, fn, feats)
        result["two_bin"] = {"feature": fn, "threshold": round(fit[1], 4),
                             "policy_low": fit[2], "policy_high": fit[3],
                             "chain_low": pols.get(fit[2], {}).get("chain"),
                             "chain_high": pols.get(fit[3], {}).get("chain"),
                             "units_low": pols.get(fit[2], {}).get("units"),
                             "units_high": pols.get(fit[3], {}).get("units"),
                             "lofo_frac_of_oracle": round(best_s, 3), "perm_p": round(p, 3),
                             "accepted": p < 0.2}
    else:
        result["two_bin"] = {"accepted": False, "reason": "no feature split beat one bin on held-out families",
                             "best_candidate": two[0][1] if two else None,
                             "best_candidate_lofo": round(two[0][0], 3) if two else None}
    result["bins"] = "two" if result["two_bin"].get("accepted") else "one"
    return result


# --------------------------------------------------------------------------- pruning rules
def pruning_rules(c: Campaign) -> dict:
    fam = _families(c)
    cut_count: dict[str, list] = defaultdict(list)
    for did in fam:
        d = c.root / "prune" / did
        if not d.exists():
            continue
        files = sorted(d.glob("r*.json"), key=lambda p: int(p.stem[1:]))
        if not files:
            continue
        last = json.loads(files[-1].read_text())
        for x in last.get("cuts", []):
            cut_count[x["factor"]].append({"design": did, "confidence": x["confidence"],
                                           "reason": x["reason"][:200]})
    audits = [r for r in c.results() if r["purpose"] == "audit" and r["design"] in fam]
    false_prunes = []
    for r in audits:
        ex = [x["d_wns"] for x in c.results(r["design"]) if x["purpose"] == "exploit"
              and x.get("round") == r.get("round") and x["valid"]]
        if r["valid"] and ex and r["d_wns"] > max(ex):
            false_prunes.append({"design": r["design"], "round": r.get("round"),
                                 "chain": r["chain_key"], "d_wns": r["d_wns"],
                                 "best_exploit": max(ex)})
    rules = []
    n = len(fam)
    for f, lst in sorted(cut_count.items(), key=lambda kv: -len(kv[1])):
        aud = [r for r in audits if any(f in step_factors(st) for st in r["chain"])]
        fp = [x for x in false_prunes if f in x["chain"]]
        rules.append({"factor": f, "cut_on": len(lst), "of_designs": n,
                      "mean_confidence": round(statistics.mean(x["confidence"] for x in lst), 2),
                      "audited": len(aud), "false_prunes": len(fp),
                      "status": ("confirmed" if len(lst) * 2 >= n and aud and not fp else
                                 "refuted" if fp else "unaudited"),
                      "example_reason": lst[0]["reason"]})
    return {"rules": rules, "audits": len(audits), "false_prunes": false_prunes,
            "false_prune_rate": round(len(false_prunes) / len(audits), 3) if audits else None}


# --------------------------------------------------------------------------- signals
def signals(c: Campaign) -> list[dict]:
    """Mid-run signal: a phys_opt step whose passes optimised 0 cells/nets gains nothing."""
    zero, zero_gain, nonzero, nonzero_gain = 0, 0, 0, 0
    for r in c.results():
        if not r["valid"] or len(r["chain"]) != 1 or r["chain"][0]["cmd"] != "phys_opt":
            continue
        pe = pass_effects(Path(r["run_dir"]))
        if not pe:
            continue
        tot = sum(v["optimized"] for v in pe.values())
        g = (r["d_wns"] or 0) > c.noise_floor(r["design"])
        if tot == 0:
            zero += 1
            zero_gain += g
        else:
            nonzero += 1
            nonzero_gain += g
    out = []
    if zero + nonzero:
        out.append({"signal": "phys_opt pass table: total optimized cells/nets == 0",
                    "meaning": "the phys_opt step had nothing to act on",
                    "n_zero": zero, "p_gain_given_zero": round(zero_gain / zero, 3) if zero else None,
                    "n_nonzero": nonzero,
                    "p_gain_given_nonzero": round(nonzero_gain / nonzero, 3) if nonzero else None,
                    "use": "stop repeating phys_opt on this state when the signal fires"})
    return out


# --------------------------------------------------------------------------- recipes
def recipes(c: Campaign) -> list[dict]:
    fam = _families(c)
    out = []
    for did in fam:
        cand_p = c.root / "discover" / did / "candidates.json"
        if not cand_p.exists():
            continue
        try:
            cands = json.loads(cand_p.read_text())
        except json.JSONDecodeError:
            continue
        for cd in cands[:3]:
            tcl_p = c.root / "discover" / did / "trials" / f"{cd.get('trial')}.tcl"
            if not tcl_p.exists():
                continue
            tag = f"{did}:{cd.get('name')}"
            own = [r for r in c.results(did) if r["purpose"] == "discover" and r.get("tag") == cd.get("trial")]
            rep = [r for r in c.results(did) if r["purpose"] == "recipe_repeat" and r.get("tag") == tag]
            tr = [r for r in c.results() if r["purpose"] == "recipe_transfer" and r.get("tag") == tag]
            gains = {r["design"]: (r["d_wns"] if r["valid"] else INVALID) for r in tr}
            helped = [d for d, g in gains.items() if g > c.noise_floor(d)]
            scope = "general" if len(helped) >= 2 else "design_specific"
            out.append({"name": cd.get("name"), "from_design": did, "tcl": tcl_p.read_text(),
                        "rationale": cd.get("rationale"), "own_d_wns": own[0]["d_wns"] if own else None,
                        "own_valid": own[0]["valid"] if own else None,
                        "repeat_d_wns": rep[0]["d_wns"] if rep else None,
                        "transfer": gains, "helped_designs": helped, "scope": scope,
                        "ship_mode": "keep_if_better"})
    return out


# --------------------------------------------------------------------------- cost model
def cost_model(c: Campaign) -> dict:
    """Per command: secs ~ a * prims^b (log-log fit over training runs)."""
    pts: dict[str, list] = defaultdict(list)
    for r in c.results():
        prims = (c.profile(r["design"])["input"] or {}).get("prims")
        if not prims or not r["valid"]:
            continue
        for s in r["steps"]:
            n = s["name"]
            cmd = ("phys_opt" if "phys_opt" in n else "place" if "place" in n else
                   "route" if "route" in n else "opt" if "opt" in n else None)
            if cmd and s["secs"] > 0.5:
                pts[cmd].append((math.log(prims), math.log(s["secs"])))
    out = {}
    for cmd, xy in pts.items():
        if len(xy) < 3:
            continue
        mx = statistics.mean(x for x, _ in xy)
        my = statistics.mean(y for _, y in xy)
        vx = sum((x - mx) ** 2 for x, _ in xy)
        b = sum((x - mx) * (y - my) for x, y in xy) / vx if vx else 0.0
        a = math.exp(my - b * mx)
        out[cmd] = {"a": round(a, 6), "b": round(b, 4), "n": len(xy)}
    return out


# --------------------------------------------------------------------------- notes
NOTE_SCHEMA = {"type": "object", "properties": {"notes": {"type": "array", "items": {
    "type": "object", "properties": {"id": {"type": "string"}, "note": {"type": "string"}},
    "required": ["id", "note"]}}}, "required": ["notes"]}


def _numbers(s: str) -> set[str]:
    return {x.rstrip(".") for x in re.findall(r"-?\d+(?:\.\d+)?", s)}


def mechanism_notes(c: Campaign, kb: dict) -> dict:
    entries = {}
    for a in kb["actions"][:25]:
        entries[f"action:{a['factor']}"] = {"profile": a["profile"], "ship_mode": a["ship_mode"]}
    for cnd in kb["conditions"]:
        if cnd["status"] == "condition":
            entries[f"condition:{cnd['factor']}"] = cnd
    for r in kb["recipes"]:
        entries[f"recipe:{r['from_design']}:{r['name']}"] = {k: r[k] for k in
                                                             ("own_d_wns", "scope", "helped_designs", "rationale")}
    if not entries:
        return {}
    prompt = ("Write a one-to-three sentence mechanism note for each KB entry below: WHY the "
              "action behaves as measured in Vivado terms (what the tool does and what design "
              "property it interacts with). Use only numbers that appear in the entry; prefer "
              "none. Entries (id: statistics):\n\n" +
              "\n".join(f"{k}: {json.dumps(v)}" for k, v in entries.items()))
    out = llm.ask(c.cfg, "distill", "notes", prompt, NOTE_SCHEMA)
    notes = {}
    for n in (out or {}).get("notes", []):
        src = json.dumps(entries.get(n["id"], {}))
        bad = [x for x in _numbers(n["note"]) if x not in src and x not in ("0", "1", "2", "3")]
        if n["id"] in entries and not bad:
            notes[n["id"]] = n["note"]
    return notes


# --------------------------------------------------------------------------- main
def run(c: Campaign, log=print) -> dict:
    rng = random.Random(7)
    fam = _families(c)
    acts = action_profiles(c)
    log(f"[distill] {len(acts)} action profiles")
    conds = conditions(c, acts, rng)
    log(f"[distill] conditions: {sum(1 for x in conds if x['status'] == 'condition')} accepted "
        f"of {len(conds)} tested")
    pol = policies(c, rng)
    log(f"[distill] policies: bins={pol.get('bins')} one-bin LOFO "
        f"{(pol.get('one_bin') or {}).get('lofo_frac_of_oracle')}")
    pr = pruning_rules(c)
    sig = signals(c)
    rec = recipes(c)
    feats = {d: c.features(d) for d in fam}
    ranges = {}
    for k in FEATURE_DOC:
        vs = [f[k] for f in feats.values() if isinstance(f.get(k), (int, float))]
        if vs:
            ranges[k] = [min(vs), max(vs)]
    negative = [{"factor": a["factor"], "why": "never gained above noise", "profile": a["profile"]}
                for a in acts if a["profile"]["n_designs"] >= 3 and a["profile"]["p_gain"] == 0]
    negative += [{"factor": a["factor"], "why": "often illegal result", "profile": a["profile"]}
                 for a in acts if (a["profile"]["validity"] or 1) < 0.5]
    negative += [{"factor": r["factor"], "why": "pruned on most designs; audits found no gain",
                  "cut_on": r["cut_on"]} for r in pr["rules"] if r["status"] == "confirmed"]
    hyps = []
    for did in fam:
        d = c.root / "prune" / did
        for f in sorted(d.glob("r*.json")) if d.exists() else []:
            for h in json.loads(f.read_text()).get("hypotheses", []):
                hyps.append({"design": did, "round": int(f.stem[1:]), **h})
    kb = {
        "meta": {"campaign": c.cfg.name, "part": c.cfg.part, "vivado": c.cfg.vivado_version,
                 "designs": sorted(fam), "families": sorted(set(fam.values())),
                 "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "n_runs": len(c.results()), "unit": "dWNS ns vs start state; MHz display only"},
        "feature_definitions": FEATURE_DOC, "feature_ranges": ranges,
        "design_features": feats,
        "actions": acts, "conditions": conds, "policies": pol,
        "pruning": pr, "negative": negative, "signals": sig, "recipes": rec,
        "hypotheses": hyps, "cost_model": cost_model(c),
        "llm_spend": llm.spend(c.cfg),
    }
    kb["notes"] = mechanism_notes(c, kb)
    kd = c.root / "kb"
    kd.mkdir(exist_ok=True)
    (kd / "kb.json").write_text(json.dumps(kb, indent=1))
    log(f"[distill] kb.json written: {len(acts)} actions, {len(negative)} negative, "
        f"{len(rec)} recipes, {len(kb['notes'])} notes")
    return kb
