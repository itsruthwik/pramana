"""Sequential training: add training designs one at a time and measure the KB as it grows.

For step n (design d, unseen by KB_{n-1}):
  pre     evaluate KB_{n-1} on d                 (runs alongside training: it only needs KB_{n-1})
  train   add d; prepare (store), prune, search, discover for d; confirm (only new
          design x policy pairs run), distill, compile -> KB_n, snapshotted as v<n>
  post    evaluate KB_n on d
  heldout every k steps (and the last), evaluate KB_n on a held-out eval campaign
The pre score is a true out-of-sample measurement at every step, so the curve of pre vs n
shows when more training stops helping. Analysts see the draft KB of all earlier designs
(evidence.kb_brief), so each new design is explored with what the earlier ones taught.

  python -m pramana.sequential <exp> <heldout_campaign> d1,d2,... [--heldout-every 3]
                               [--budget 3000] [--prior-kb path/to/kb.json]
Results: campaigns/<exp>/sequential.json (one record per step), sequential.md (table).
"""
from __future__ import annotations

import argparse
import json
import shutil
import threading
import time
from pathlib import Path

from . import corpus, evaluate
from .campaign import Campaign
from .cli import PREPARE, log_to, stage
from .config import Config

TRAIN = ["prune", "search", "discover", "confirm", "distill", "compile"]


def _ensure(name: str, base: Config | None = None, **kw) -> Config:
    p = Config(name=name).root / "campaign.json"
    if p.exists():
        return Config.load(name)
    cfg = Config(name=name, designs=[], **kw)
    if base:
        for k in ("part", "vivado_settings", "vivado_version", "threads", "llm_model",
                  "llm_effort", "rounds", "runs_per_design_per_round", "discover_trials"):
            setattr(cfg, k, getattr(base, k))
    cfg.save()
    corpus.save([], cfg.root)
    return cfg


def add_design(cfg: Config, did: str) -> None:
    ds = corpus.load(cfg.root)
    if did not in {d["id"] for d in ds}:
        ds += corpus.register(only=[did])
        corpus.save(ds, cfg.root)
    if did not in cfg.designs:
        cfg.designs.append(did)
        cfg.save()


def snapshot(cfg: Config, n: int) -> Path:
    """Freeze KB_n: kb/kb_v<n>.json and out_v<n>/ (bundles)."""
    shutil.copy2(cfg.root / "kb" / "kb.json", cfg.root / "kb" / f"kb_v{n}.json")
    dst = cfg.root / f"out_v{n}"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(cfg.root / "out", dst)
    return dst / "mithya"


def _baseline_best(c: Campaign, did: str) -> float | None:
    v = [r["d_wns"] for r in c.results(did) if r["purpose"] == "baseline" and r["valid"]
         and not r.get("tag", "").endswith("_rep")]
    return max(v) if v else None


def _eval_one(train: Campaign, ev: Campaign, bundle: Path, did: str, label: str,
              budget: float, log) -> dict | None:
    rows = evaluate.run(train, ev, budget, log=log, bundle=bundle, designs=[did], label=label)
    return rows[0] if rows else None


def write_table(root: Path, steps: list[dict]) -> None:
    L = ["# Sequential training", "",
         "pre = KB from earlier designs only (out-of-sample); post = KB after training on it; "
         "baseline = best single Explore run; oracle = best found in training. dWNS in ns.", "",
         "| n | design | baseline | pre | post | oracle | pre/oracle | KB policy | heldout mean |",
         "|--|--|--|--|--|--|--|--|--|"]
    for s in steps:
        o = s.get("oracle")
        pre = s.get("pre_d_wns")
        frac = f"{pre / o:.2f}" if pre is not None and o else ""
        ho = s.get("heldout_mean")
        L.append(f"| {s['n']} | {s['design']} | {s.get('baseline')} | {pre} | {s.get('post_d_wns')} "
                 f"| {o} | {frac} | {s.get('policy', '')[:60]} | {'' if ho is None else ho} |")
    (root / "sequential.md").write_text("\n".join(L) + "\n")


def run(exp: str, heldout: str, order: list[str], heldout_every: int = 3,
        budget: float = 3000, prior_kb: str = "", rounds: int = 3, runs_per_round: int = 8,
        discover_trials: int = 6) -> None:
    base = Config.load(heldout)
    tcfg = _ensure(exp, base, prior_kb=prior_kb)
    if not (tcfg.root / "sequential.json").exists():   # settings fixed at the first step
        tcfg.rounds, tcfg.runs_per_design_per_round = rounds, runs_per_round
        tcfg.discover_trials, tcfg.workers = discover_trials, 4
        tcfg.save()
    ecfg = _ensure(f"{exp}_ev", base, workers=2)
    log = log_to(tcfg)
    out_path = tcfg.root / "sequential.json"
    steps = json.loads(out_path.read_text()) if out_path.exists() else []
    done = {s["n"] for s in steps if s.get("complete")}
    ho = Campaign(Config.load(heldout))
    for n, did in enumerate(order):
        if n in done:
            continue
        t0 = time.time()
        log(f"##### step {n}: {did} #####")
        rec = {"n": n, "design": did, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        add_design(tcfg, did)
        add_design(ecfg, did)
        for s in PREPARE:
            stage(s, tcfg, log)
        stage("profile", ecfg, log)            # store hit: same DCP
        train, ev = Campaign(Config.load(exp)), Campaign(Config.load(f"{exp}_ev"))
        rec["baseline"] = _baseline_best(train, did)
        # pre: KB_{n-1} on the new design, in parallel with training
        pre_box: dict = {}
        pre_thread = None
        prev_bundle = tcfg.root / f"out_v{n - 1}" / "mithya"
        if n > 0 and prev_bundle.exists():
            pre_thread = threading.Thread(target=lambda: pre_box.update(
                r=_eval_one(train, ev, prev_bundle, did, f"{exp}_v{n - 1}", budget, log)))
            pre_thread.start()
        for s in TRAIN:
            stage(s, Config.load(exp), log)
        bundle = snapshot(Config.load(exp), n)
        train = Campaign(Config.load(exp))
        kb = json.loads((tcfg.root / "kb" / f"kb_v{n}.json").read_text())
        pol = (kb.get("policies") or {}).get("one_bin") or {}
        rec.update(policy=pol.get("policy", ""), lofo=pol.get("lofo_frac_of_oracle"),
                   bins=(kb.get("policies") or {}).get("bins"),
                   oracle=round(train.best_state(did)["wns"] - train.input_state(did)["wns"], 4))
        if pre_thread:
            pre_thread.join()
            r = pre_box.get("r")
            rec.update(pre_d_wns=r and r["d_wns"], pre_valid=r and r["valid"], pre_secs=r and r["secs"])
        post = _eval_one(train, ev, bundle, did, f"{exp}_v{n}", budget, log)
        rec.update(post_d_wns=post and post["d_wns"], post_valid=post and post["valid"],
                   post_secs=post and post["secs"])
        if heldout_every and ((n + 1) % heldout_every == 0 or n == len(order) - 1):
            rows = evaluate.run(train, ho, budget, log=log, bundle=bundle, label=f"{exp}_v{n}")
            rec["heldout"] = {r["design"]: r["d_wns"] for r in rows}
            vals = [r["d_wns"] for r in rows if r["valid"] and r["d_wns"] is not None]
            rec["heldout_mean"] = round(sum(vals) / len(vals), 4) if vals else None
        rec.update(complete=True, wall_h=round((time.time() - t0) / 3600, 2))
        steps = [s for s in steps if s["n"] != n] + [rec]
        steps.sort(key=lambda s: s["n"])
        out_path.write_text(json.dumps(steps, indent=1))
        write_table(tcfg.root, steps)
        log(f"##### step {n} done: pre={rec.get('pre_d_wns')} post={rec.get('post_d_wns')} "
            f"oracle={rec.get('oracle')} baseline={rec.get('baseline')} "
            f"({rec['wall_h']} h) #####")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="pramana.sequential")
    ap.add_argument("exp")
    ap.add_argument("heldout")
    ap.add_argument("order")
    ap.add_argument("--heldout-every", type=int, default=3)
    ap.add_argument("--budget", type=float, default=3000)
    ap.add_argument("--prior-kb", default="")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--runs-per-round", type=int, default=8)
    ap.add_argument("--discover-trials", type=int, default=6)
    a = ap.parse_args()
    run(a.exp, a.heldout, a.order.split(","), a.heldout_every, a.budget, a.prior_kb,
        a.rounds, a.runs_per_round, a.discover_trials)
