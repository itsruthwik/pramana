"""Incremental training: grow one campaign a batch of designs at a time, with a full KB
snapshot after every batch.

    python -m pramana.incremental <campaign> --dcp-list dcp_database/split_train.csv \
        [--designs id1,id2,...] [--step 10] [--batches 1]

A new campaign is created with --llm-model / --workers (other settings default); an existing
one keeps its campaign.json settings. The order of designs is the order in --designs, else the CSV's order.
Each batch:
  1. adds the next `step` designs (corpus + campaign.json);
  2. resets S6's frozen policy set, so confirm covers every design so far (runs already done
     are not repeated: run ids are design + start + chain);
  3. runs all stages (prepare .. compile); work already done is skipped;
  4. snapshots the KB: snapshots/n<total designs>/ with kb.json and out/ (bundles), plus the
     batch's time and LLM spend in incremental.json.
--batches N stops after N batches, so a snapshot can be evaluated before going on; run the
same command again to continue (an interrupted batch resumes where it stopped). Evaluate a
snapshot with:  python -m pramana.evaluate <campaign> <eval_campaign> 3000 \
                    campaigns/<campaign>/snapshots/n10/out/mithya
New designs' analysts see the KB of the earlier batches (evidence.kb_brief), as in
sequential training.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from . import corpus, llm
from .cli import ALL, log_to, stage, time_line
from .config import Config


def extend(cfg: Config, new: list[dict], log=print) -> None:
    """Add designs to a campaign and reset S6's frozen policy set and start time."""
    ds = corpus.load(cfg.root) if (cfg.root / "designs.json").exists() else []
    have = {d["id"] for d in ds}
    ds += [d for d in new if d["id"] not in have]
    corpus.save(ds, cfg.root)
    cfg.designs = [d["id"] for d in ds]
    cfg.save()
    for f in ("policies.frozen.json", "confirm_state.json"):
        p = cfg.root / f
        if p.exists():
            p.rename(cfg.root / f"{f}.n{len(have)}")      # kept, not deleted
    log(f"[incremental] campaign now has {len(ds)} designs (+{len(ds) - len(have)})")


def snapshot(cfg: Config, n: int) -> Path:
    dst = cfg.root / "snapshots" / f"n{n}"
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    shutil.copy2(cfg.root / "kb" / "kb.json", dst / "kb.json")
    shutil.copytree(cfg.root / "out", dst / "out")
    return dst


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="pramana.incremental")
    ap.add_argument("campaign")
    ap.add_argument("--dcp-list", required=True)
    ap.add_argument("--designs", default="", help="ids in training order (default: CSV order)")
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--batches", type=int, default=0, help="stop after this many (0 = all)")
    ap.add_argument("--llm-model", default="", help="new campaign only, e.g. codex:gpt-6.1-terra")
    ap.add_argument("--workers", type=int, default=0, help="Vivado runs in parallel (saved)")
    a = ap.parse_args(argv)
    if not (Config(name=a.campaign).root / "campaign.json").exists():
        cfg = Config(name=a.campaign, designs=[])
        if a.llm_model:
            cfg.llm_model = a.llm_model
        cfg.save()
        corpus.save([], cfg.root)
    cfg = Config.load(a.campaign)
    if a.workers:
        cfg.workers = a.workers
        cfg.save()
    log = log_to(cfg)
    pool = corpus.register_list(Path(a.dcp_list))
    if a.designs:
        by = {d["id"]: d for d in pool}
        missing = [x for x in a.designs.split(",") if x not in by]
        if missing:
            raise SystemExit(f"not in {a.dcp_list}: {missing}")
        pool = [by[x] for x in a.designs.split(",")]
    sp = cfg.root / "incremental.json"
    st = json.loads(sp.read_text()) if sp.exists() else {"step": a.step, "batches": []}
    done_batches = 0
    while True:
        open_b = next((b for b in st["batches"] if not b.get("finished")), None)
        if open_b is None:
            have = {x for b in st["batches"] for x in b["designs"]}
            todo = [d for d in pool if d["id"] not in have][:st["step"]]
            if not todo:
                log("[incremental] all designs trained")
                break
            if a.batches and done_batches >= a.batches:
                log(f"[incremental] stopping after {done_batches} batch(es); run again to continue")
                break
            open_b = {"k": len(st["batches"]) + 1, "designs": [d["id"] for d in todo],
                      "started": time.strftime("%Y-%m-%d %H:%M:%S")}
            extend(cfg, todo, log)
            st["batches"].append(open_b)
            sp.write_text(json.dumps(st, indent=1))
        elif a.batches and done_batches >= a.batches:
            break
        cfg = Config.load(a.campaign)
        n = len(cfg.designs)
        log(f"##### incremental batch {open_b['k']}: {len(open_b['designs'])} designs "
            f"-> {n} total #####")
        t0 = time.time()
        for s in ALL:
            stage(s, Config.load(a.campaign), log)
        cfg = Config.load(a.campaign)
        dst = snapshot(cfg, n)
        sp_now = llm.spend(cfg).get("TOTAL", {})
        open_b.update(finished=time.strftime("%Y-%m-%d %H:%M:%S"),
                      batch_h=round((time.time() - t0) / 3600, 2), n_designs=n,
                      snapshot=str(dst), llm_usd_total=sp_now.get("est_usd"),
                      time=time_line(cfg))
        sp.write_text(json.dumps(st, indent=1))
        log(f"##### batch {open_b['k']} done in {open_b['batch_h']} h: KB at {n} designs -> {dst} #####")
        log(open_b["time"])
        done_batches += 1


if __name__ == "__main__":
    main()
