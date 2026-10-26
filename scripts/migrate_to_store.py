"""One-off: move framework-level work of existing campaigns into the store.

Copies profiles and finished reference (baseline) runs; publishes the probed space spec.
Campaign files are left untouched (run paths keep pointing into the campaign dirs).
  python3 scripts/migrate_to_store.py dev1 dev2 eval1
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pramana import corpus, store                     # noqa: E402
from pramana.config import Config                     # noqa: E402

for name in sys.argv[1:]:
    cfg = Config.load(name)
    designs = {d["id"]: d for d in corpus.load(cfg.root)}
    n_prof = 0
    for did, d in designs.items():
        src, dst = cfg.root / "profile" / did, store.profile_dir(cfg, d)
        if (src / "profile.json").exists() and not dst.exists():
            shutil.copytree(src, dst)
            n_prof += 1
    root = store.runs_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    db = root / "results.jsonl"
    have = {json.loads(l)["run_id"] for l in db.read_text().splitlines()} if db.exists() else set()
    n_runs = 0
    with db.open("a") as f:
        for l in (cfg.root / "results.jsonl").read_text().splitlines():
            r = json.loads(l)
            if r["purpose"] != "baseline" or r["status"] not in ("ok", "error") \
                    or r["run_id"] in have or r["design"] not in designs \
                    or r.get("threads", cfg.threads) != cfg.threads and r.get("rep", 0) == 0:
                continue
            r["dcp_md5"] = designs[r["design"]]["md5"]
            f.write(json.dumps(r) + "\n")
            have.add(r["run_id"])
            n_runs += 1
    if (cfg.root / "space.json").exists():
        store.publish_spec(cfg, cfg.root)
    print(f"{name}: {n_prof} profiles, {n_runs} reference runs -> store")
