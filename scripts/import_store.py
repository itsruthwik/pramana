#!/usr/bin/env python3
"""Copy another pramana store's framework work (profile, input check, baseline runs) for a set
of DCPs into this repo's store, and record how long that work took so it can be added to
training time.

    python3 scripts/import_store.py <src_store> <dcp_list.csv> [--src-log <campaign>/pramana.log]

Only DCPs whose md5 matches are copied. Paths inside copied results rows are rewritten to
this store. Rows already here (same run_id) are skipped, so the script can be re-run.

Time record (store/imported.json): per design the Vivado seconds of its copied baseline and
noise runs, and, from the source campaign's log, the wall time of its profile and baseline
stages. `pramana status` and the end of `pramana all` add that wall time to the campaign's
own time ("training time incl. reused prepare").
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pramana import corpus, store  # noqa: E402
from pramana.config import Config  # noqa: E402


def stage_wall(log: Path) -> dict:
    """Seconds per stage from '=== <stage> done in Ns ===' lines (summed over restarts)."""
    out: dict = {}
    for m in re.finditer(r"=== (\w+) done in (\d+)s ===", log.read_text(errors="replace")):
        out[m.group(1)] = out.get(m.group(1), 0) + int(m.group(2))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src_store", type=Path)
    ap.add_argument("dcp_list", type=Path)
    ap.add_argument("--src-log", type=Path, default=None)
    ap.add_argument("--version", default="2025.2")
    ap.add_argument("--threads", type=int, default=4)
    a = ap.parse_args()
    cfg = Config(name="_import", vivado_version=a.version, threads=a.threads)
    designs = corpus.register_list(a.dcp_list)
    by_md5 = {d["md5"]: d for d in designs}
    src_runs = a.src_store / f"vivado_{a.version}" / f"t{a.threads}"
    dst_runs = store.runs_root(cfg)
    dst_runs.mkdir(parents=True, exist_ok=True)

    # profile + input check
    n_dirs = 0
    for d in designs:
        src = a.src_store / "dcp" / store.dcp_key(d)
        dst = store.STORE / "dcp" / store.dcp_key(d)
        if src.exists() and not dst.exists():
            shutil.copytree(src, dst)
            n_dirs += 1

    # baseline rows and their run dirs
    have = {json.loads(l)["run_id"] for l in (dst_runs / "results.jsonl").read_text().splitlines()
            if l.strip()} if (dst_runs / "results.jsonl").exists() else set()
    src_s, dst_s = str(a.src_store.resolve()), str(store.STORE)
    new, vivado_s = [], {}
    for line in (src_runs / "results.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        d = by_md5.get(r.get("dcp_md5"))
        if d is None or r["run_id"] in have:
            continue
        r["design"] = d["id"]
        src_dir = Path(r["run_dir"])
        dst_dir = dst_runs / "runs" / d["id"] / r["run_id"]
        if src_dir.exists() and not dst_dir.exists():
            shutil.copytree(src_dir, dst_dir)
        line = json.dumps(r).replace(str(src_dir), str(dst_dir)).replace(src_s, dst_s)
        new.append(line)
        vivado_s[d["id"]] = vivado_s.get(d["id"], 0) + (r.get("wall_s") or 0)
    with open(dst_runs / "results.jsonl", "a") as f:
        for line in new:
            f.write(line + "\n")

    rec_p = store.STORE / "imported.json"
    rec = json.loads(rec_p.read_text()) if rec_p.exists() else {"imports": []}
    walls = stage_wall(a.src_log) if a.src_log else {}
    rec["imports"].append({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"), "source": str(a.src_store.resolve()),
        "source_log": str(a.src_log) if a.src_log else None,
        "designs": sorted(vivado_s) or [d["id"] for d in designs],
        "profile_wall_s": walls.get("profile", 0), "baseline_wall_s": walls.get("baseline", 0),
        "vivado_s": {k: round(v) for k, v in vivado_s.items()}})
    rec_p.write_text(json.dumps(rec, indent=1))
    print(f"copied {n_dirs} profile/input dirs, {len(new)} result rows for {len(vivado_s)} designs "
          f"({round(sum(vivado_s.values()) / 3600, 1)} Vivado-h); source wall: profile "
          f"{walls.get('profile', 0)} s, baseline {walls.get('baseline', 0)} s")


if __name__ == "__main__":
    main()
