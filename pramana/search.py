"""S4: search rounds inside each design's pruned space.

Each round, per design:
  start  = best legal state so far (chains grow one round at a time: singles -> stacks -> deep)
  exploit (~70%): factored UCB bandit over candidates (LLM proposals + generated points),
                  blended with the LLM's predicted dWNS by a weight earned from its accuracy
  test    (~20%): hypothesis test chains, then the shared floor (every directive single, run
                  from the input DCP so the same action is measured on every design)
  audit   (~10%): random points that use a pruned-out factor (measures false prunes)
Then: run, score LLM predictions, write the checkpoint report, re-prune with the new results.
A design stops after two rounds without a gain above its noise floor.
"""
from __future__ import annotations

import json
import math
import random
from concurrent.futures import ThreadPoolExecutor

from . import evidence, llm, prune, space
from .campaign import Campaign
from .space import chain_key, step_factors

INVALID_PENALTY = 0.05     # ns-equivalent penalty for an illegal result
UCB_C = 0.5
OTHER_DESIGN_WEIGHT = 0.25


# --------------------------------------------------------------------------- state
def _state_path(c: Campaign):
    return c.root / "search_state.json"


def load_state(c: Campaign) -> dict:
    p = _state_path(c)
    if p.exists():
        return json.loads(p.read_text())
    return {"round": 0, "llm_weight": 0.6, "best_history": {}, "stopped": [], "pred_pairs": []}


def save_state(c: Campaign, st: dict) -> None:
    _state_path(c).write_text(json.dumps(st, indent=1))


# --------------------------------------------------------------------------- bandit
def reward(r: dict) -> float:
    if not r["valid"]:
        return -INVALID_PENALTY + min(r["d_wns"] or 0.0, 0.0)
    return r["d_wns"] or 0.0


def factor_stats(c: Campaign, did: str, from_input: bool) -> dict[str, list[float]]:
    """factor -> [weighted sum, weight] over runs that start from the same kind of state as
    the candidates (input DCP vs an improved state): a re-place gains a lot from the input
    but usually loses from a tuned state, so the two must not be mixed.
    This design weight 1, others OTHER_DESIGN_WEIGHT."""
    stats: dict[str, list[float]] = {}
    for r in c.results():
        if r.get("extra_tcl") or (r["start"] == "input") != from_input:
            continue
        w = 1.0 if r["design"] == did else OTHER_DESIGN_WEIGHT
        rw = reward(r)
        fs = set()
        for st in r["chain"]:
            fs.update(step_factors(st))
        for f in fs:
            s = stats.setdefault(f, [0.0, 0.0])
            s[0] += w * rw
            s[1] += w
    return stats


def bandit_parts(chain: list[dict], stats: dict, n_total: float, scale: float) -> tuple[float, float]:
    """(value estimate, UCB exploration bonus); bonus is on the scale of typical gains."""
    fs = set()
    for st in chain:
        fs.update(f for f in step_factors(st) if ":" in f)
    if not fs:
        return 0.0, 0.0
    means, bonus = [], []
    for f in fs:
        s, w = stats.get(f, (0.0, 0.0))
        means.append(s / w if w else 0.0)
        bonus.append(math.sqrt(math.log(n_total + 2) / (w + 1)))
    return sum(means) / len(means), UCB_C * scale * sum(bonus) / len(bonus)


# --------------------------------------------------------------------------- candidates
def enumerate_allowed(spec: dict, allowed: dict) -> list[dict]:
    out = []
    for st in space.enumerate_simple_steps(spec, cmd_filter=set(allowed)):
        if prune.in_allowed(st, allowed):
            out.append(st)
    # route steps: also the +ripup variant of each kept route directive
    if "route" in allowed and "+ripup" in allowed["route"]["options"]:
        for d in allowed["route"]["directives"]:
            out.append({"cmd": "route", "directive": d, "opts": {"+ripup": True}})
    return out


def audit_step(spec: dict, cuts: list[dict], rng: random.Random) -> dict | None:
    for _ in range(30):
        f = rng.choice(cuts)["factor"]
        cmd, _, rest = f.partition(":")
        if not rest:
            st = space.random_step(spec, rng, allowed={cmd: {
                "directives": list(spec["commands"][cmd]["directives"]),
                "options": {k: (True if o["type"] == "switch" else o["values"])
                            for k, o in spec["commands"][cmd]["options"].items()}}})
        elif rest.startswith("directive="):
            st = {"cmd": cmd, "directive": rest.split("=", 1)[1]}
        else:
            flag, _, val = rest.partition("=")
            o = spec["commands"][cmd]["options"].get(flag)
            if o is None:
                continue
            if o["type"] == "switch":
                st = {"cmd": cmd, "opts": {flag: True}}
            elif o["type"] == "multi":
                st = {"cmd": cmd, "opts": {flag: [val]}}
            elif o["type"] == "numeric":
                st = {"cmd": cmd, "opts": {flag: int(val)}}
            else:
                st = {"cmd": cmd, "opts": {flag: val}}
        if st and space.legal(st, spec) and space.chain_ok([st], spec):
            return st
    return None


def allocate(c: Campaign, did: str, pr: dict, start: dict, rnd: int, w_llm: float,
             rng: random.Random) -> tuple[list[dict], list[dict]]:
    cfg = c.cfg
    spec = c.spec()
    n = cfg.runs_per_design_per_round
    n_aud = max(1, round(n * cfg.share_audit))
    n_test = max(1, round(n * cfg.share_test))
    n_exp = max(1, n - n_test - n_aud)
    done = {(r["start"], r["chain_key"]) for r in c.results(did)}
    from_input = start["id"] == "input"
    stats = factor_stats(c, did, from_input)
    n_total = sum(w for _, w in stats.values())
    gains = [abs(r["d_wns"]) for r in c.results(did) if r["valid"] and r["d_wns"] is not None
             and (r["start"] == "input") == from_input]
    scale = sorted(gains)[len(gains) // 2] if gains else 0.03
    scale = min(max(scale, 0.01), 0.1)

    # ---- exploit
    cands: dict[str, dict] = {}
    for p in pr.get("proposals", []):
        if not p.get("in_allowed", True):
            continue
        k = chain_key(p["chain"])
        cands[k] = {"chain": p["chain"], "pred": p["predicted_d_wns"], "src": "llm"}
    for st in enumerate_allowed(spec, pr["allowed"]):
        cands.setdefault(chain_key([st]), {"chain": [st], "pred": None, "src": "grid"})
    for _ in range(20):
        st = space.random_step(spec, rng, allowed=pr["allowed"])
        if st:
            cands.setdefault(chain_key([st]), {"chain": [st], "pred": None, "src": "random_in"})
    scored = []
    for k, cd in cands.items():
        if (start["id"], k) in done or not space.chain_ok(cd["chain"], spec):
            continue
        v, bonus = bandit_parts(cd["chain"], stats, n_total, scale)
        s = ((1 - w_llm) * v + w_llm * cd["pred"] if cd["pred"] is not None else v) + bonus
        scored.append((s, k, cd))
    scored.sort(key=lambda x: -x[0])
    jobs, chosen, per_cmd = [], set(), {}
    cap = max(1, (n_exp + 1) // 2)
    # one exploit slot is always the bandit's own best non-LLM pick (keeps LLM vs bandit
    # comparable and stops the LLM from starving unproposed regions)
    bandit_pick = next(((s, k, cd) for s, k, cd in scored if cd["src"] != "llm"), None)
    if bandit_pick and n_exp >= 2:
        s0, k0, cd0 = bandit_pick
        chosen.add(k0)
        per_cmd[cd0["chain"][0]["cmd"]] = 1
        jobs.append(c.job(did, start, cd0["chain"], "exploit", round=rnd, tag="bandit",
                          score=round(s0, 4)))
    # beam: one exploit slot continues from the second-best state (bandit-scored), so the
    # search doesn't stall on a single lineage
    tops = c.top_states(did, 2)
    if len(tops) > 1 and n_exp >= 4 and tops[1]["id"] != start["id"]:
        s2 = tops[1]
        done2 = {k for (sid, k) in done if sid == s2["id"]}
        alt = []
        for st in enumerate_allowed(spec, pr["allowed"]):
            k2 = chain_key([st])
            if k2 in done2 or not space.chain_ok([st], spec):
                continue
            v, b = bandit_parts([st], stats, n_total, scale)
            alt.append((v + b, k2, st))
        if alt:
            alt.sort(key=lambda x: -x[0])
            jobs.append(c.job(did, s2, [alt[0][2]], "exploit", round=rnd, tag="beam2",
                              score=round(alt[0][0], 4)))
    for s, k, cd in scored:
        if k in chosen:
            continue
        if len(jobs) >= n_exp:
            break
        prim = cd["chain"][0]["cmd"]
        if per_cmd.get(prim, 0) >= cap:
            continue
        per_cmd[prim] = per_cmd.get(prim, 0) + 1
        chosen.add(k)
        jobs.append(c.job(did, start, cd["chain"], "exploit", round=rnd, tag=cd["src"],
                          score=round(s, 4)))
    # ---- test: hypothesis chains, then the shared floor from the input DCP
    tests = []
    for h in pr.get("hypotheses", []):
        if h.get("test_chain"):
            k = chain_key(h["test_chain"])
            if (start["id"], k) not in done and k not in chosen and space.chain_ok(h["test_chain"], spec):
                tests.append(c.job(did, start, h["test_chain"], "test", round=rnd, tag="hypothesis"))
    inp = c.input_state(did)
    floor = [{"cmd": cm, "directive": d} for cm, cc in spec["commands"].items()
             for d in cc["directives"] if d not in ("Quick", "RuntimeOptimized", "Default")]
    coverage = {}
    for r in c.results():
        if r["start"] == "input" and len(r["chain"]) == 1:
            coverage[r["chain_key"]] = coverage.get(r["chain_key"], 0) + 1
    rng.shuffle(floor)
    floor.sort(key=lambda st: coverage.get(chain_key([st]), 0))
    for st in floor:
        if ("input", chain_key([st])) not in done:
            tests.append(c.job(did, inp, [st], "test", round=rnd, tag="floor"))
    # interleave hypothesis tests and floor runs so both get budget
    hyp = [t for t in tests if t["tag"] == "hypothesis"]
    flr = [t for t in tests if t["tag"] == "floor"]
    mixed = [x for pair in zip(hyp, flr) for x in pair] + hyp[len(flr):] + flr[len(hyp):]
    jobs += mixed[:n_test]
    # ---- audit: pruned-out points
    if pr.get("cuts"):
        for _ in range(n_aud):
            st = audit_step(spec, pr["cuts"], rng)
            if st and (start["id"], chain_key([st])) not in done:
                jobs.append(c.job(did, start, [st], "audit", round=rnd, tag="audit"))
    preds = [{"design": did, "start": start["id"], "chain_key": k, "pred": cd["pred"]}
             for s, k, cd in scored if cd["pred"] is not None and k in chosen]
    return jobs, preds


# --------------------------------------------------------------------------- scoring
def spearman(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 4:
        return None
    def ranks(xs):
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        r = [0.0] * len(xs)
        for rank, i in enumerate(order):
            r[i] = rank
        return r
    a, b = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    n = len(pairs)
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((y - mb) ** 2 for y in b))
    return cov / (va * vb) if va and vb else None


def score_predictions(c: Campaign, preds: list[dict]) -> list[tuple[float, float]]:
    by = {(r["design"], r["start"], r["chain_key"]): r for r in c.results()}
    pairs = []
    for p in preds:
        r = by.get((p["design"], p["start"], p["chain_key"]))
        if r and r["d_wns"] is not None:
            pairs.append((p["pred"], reward(r)))
    return pairs


# --------------------------------------------------------------------------- reprune context
def audit_summary(c: Campaign, did: str) -> str:
    rs = [r for r in c.results(did) if r["purpose"] == "audit"]
    if not rs:
        return "No audit runs yet."
    best_ex = max([r["d_wns"] or -9 for r in c.results(did) if r["purpose"] == "exploit"
                   and r["valid"]] or [0])
    lines = [f"Audits (pruned-out points): {len(rs)} runs; best exploit dWNS so far {best_ex}"]
    for r in rs:
        lines.append(f"  round {r['round']}: {r['chain_key']} valid={r['valid']} dWNS={r['d_wns']}")
    return "\n".join(lines)


def reprune_context(c: Campaign, did: str, prev: dict, st: dict) -> str:
    cuts = "\n".join(f"  {x['factor']} ({x['confidence']}): {x['reason'][:160]}" for x in prev["cuts"])
    pairs = st.get("pred_pairs", [])
    rho = spearman([tuple(p) for p in pairs])
    return (f"## Results so far on this design (dWNS is vs each run's start state)\n"
            f"{evidence.results_table(c, did)}\n\n## {audit_summary(c, did)}\n\n"
            f"## Your previous prune (round {prev['round']})\nDiagnosis: {prev['diagnosis']}\n"
            f"Cuts:\n{cuts}\n\n## Your prediction accuracy so far: Spearman rho = "
            f"{None if rho is None else round(rho, 2)} over {len(pairs)} predictions. Your "
            f"weight in run selection is {st['llm_weight']:.2f}.\n\n"
            f"Re-prune: give the complete new list of cuts (factors you now want searched again "
            f"go in `uncuts`). Proposals start from the start state below (the best so far), so "
            f"they extend the current best chain.")


# --------------------------------------------------------------------------- report
REPORT_SCHEMA = {"type": "object", "properties": {"report_markdown": {"type": "string"}},
                 "required": ["report_markdown"]}


def checkpoint_report(c: Campaign, rnd: int, st: dict) -> None:
    parts = [f"# Search round {rnd} checkpoint\n"]
    for d in c.designs():
        did = d["id"]
        best = c.best_state(did)
        inp = c.input_state(did)
        parts.append(f"## {did} (family {d['family']})\ninput WNS {inp['wns']} -> best {best['wns']} "
                     f"(+{round(best['wns'] - inp['wns'], 3)} ns) via: "
                     f"{' > '.join(c.lineage(did, best['id'])) or '(none)'}\n"
                     f"{evidence.results_table(c, did, limit=40)}\n{audit_summary(c, did)}\n")
        pr = prune.latest(c, did)
        if pr:
            parts.append(f"Latest diagnosis: {pr['diagnosis'][:600]}\nKept "
                         f"{pr['kept_factors']}/{pr['total_factors']} factors.\n")
    rho = spearman([tuple(p) for p in st.get("pred_pairs", [])])
    parts.append(f"LLM prediction Spearman rho (cumulative): {rho}; LLM weight {st['llm_weight']:.2f}")
    data = "\n".join(parts)
    prompt = (data + "\n\nWrite a concise checkpoint report (markdown) for the researcher: what "
              "was learned this round across designs, which actions help on which kinds of design "
              "and which don't, whether pruning looks right (audits), surprises, and what the next "
              "round should focus on. Cite numbers from the data; invent none. Under 700 words.")
    out = llm.ask(c.cfg, "report", f"round{rnd}", prompt, REPORT_SCHEMA)
    rd = c.root / "reports"
    rd.mkdir(exist_ok=True)
    body = out["report_markdown"] if out else "(LLM report failed)"
    (rd / f"round_{rnd}.md").write_text(body + "\n\n---\n## Data\n\n" + data)


# --------------------------------------------------------------------------- main loop
def run(c: Campaign, log=print) -> None:
    """Rounds per design: each design gets cfg.rounds rounds (or stops early). Designs added
    to a campaign later (sequential training) start at their own round 1."""
    st = load_state(c)
    designs = [d["id"] for d in c.designs()]
    dr = st.setdefault("design_round", {})
    for d in designs:   # campaigns from before per-design rounds: all had st["round"] rounds
        dr.setdefault(d, st["round"] if d in st["best_history"] else 0)
    st.setdefault("iteration", st["round"])
    while True:
        active = [d for d in designs if d not in st["stopped"] and dr[d] < c.cfg.rounds]
        if not active:
            log("[search] no active designs")
            break
        it = st["iteration"] + 1
        rng = random.Random(1234 + it)
        rnd_of = {d: dr[d] + 1 for d in active}
        log(f"[search] iteration {it}: {len(active)} active designs "
            f"(rounds {sorted(set(rnd_of.values()))}), LLM weight {st['llm_weight']:.2f}")

        def get_prune(did):
            rnd = rnd_of[did]
            prev = prune.latest(c, did)
            if prev is None:
                return prune.prune_design(c, did, 0, c.best_state(did))
            if rnd == 1 and prev["round"] == 0:
                return prev
            if prev["round"] >= rnd:
                return prev
            return prune.prune_design(c, did, rnd, c.best_state(did),
                                      extra=reprune_context(c, did, prev, st), prev=prev)
        with ThreadPoolExecutor(4) as ex:
            prunes = dict(zip(active, ex.map(get_prune, active)))
        jobs, preds = [], []
        for did in active:
            j, p = allocate(c, did, prunes[did], c.best_state(did), rnd_of[did],
                            st["llm_weight"], rng)
            jobs += j
            preds += p
            log(f"[search]   {did} r{rnd_of[did]}: kept {prunes[did]['kept_factors']}/"
                f"{prunes[did]['total_factors']} factors; {len(j)} runs "
                f"({', '.join(x['purpose'] for x in j)})")
        before = {did: c.best_state(did)["wns"] for did in active}
        c.runner.run_many(jobs, log=log)
        pairs = score_predictions(c, preds)
        st["pred_pairs"] += [list(p) for p in pairs]
        rho = spearman([tuple(p) for p in st["pred_pairs"]])
        if rho is not None:
            st["llm_weight"] = min(0.9, max(0.05, rho))   # trust = measured rank correlation
        for did in active:
            dr[did] = rnd_of[did]
            after = c.best_state(did)["wns"]
            hist = st["best_history"].setdefault(did, [])
            hist.append(round(after - before[did], 4))
            nf = c.noise_floor(did)
            if len(hist) >= 2 and all(h <= nf for h in hist[-2:]):
                st["stopped"].append(did)
                log(f"[search]   {did}: stopped (no gain above {nf} ns for 2 rounds)")
        st["iteration"] = it
        st["round"] = max(dr.values())
        save_state(c, st)
        log(f"[search] iteration {it} done; prediction rho={rho}; writing checkpoint report")
        checkpoint_report(c, it, st)
