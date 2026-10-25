"""pramana command line.

  python -m pramana init <campaign> [--designs a,b,...] [--workers N] [--threads N]
  python -m pramana space <campaign>        S0  build the search-space spec
  python -m pramana probe <campaign>        S0  capability probe (directive validity on this part)
  python -m pramana profile <campaign>      S1
  python -m pramana baseline <campaign>     S2
  python -m pramana prune <campaign>        S3  (LLM) initial prune per design
  python -m pramana search <campaign>       S4  rounds (re-prune, allocate, run, settle)
  python -m pramana discover <campaign>     S5  (LLM agent) custom Tcl per design
  python -m pramana confirm <campaign>      S6
  python -m pramana distill <campaign>      S7
  python -m pramana compile <campaign>      S8
  python -m pramana all <campaign>          S0..S8
  python -m pramana status <campaign>
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .config import Config


def log_to(cfg: Config):
    path = cfg.root / "pramana.log"

    def log(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with path.open("a") as f:
            f.write(line + "\n")
    return log


def cmd_init(a) -> None:
    from . import corpus
    cfg = Config(name=a.campaign, workers=a.workers, threads=a.threads,
                 designs=a.designs.split(",") if a.designs else [])
    cfg.save()
    ds = corpus.register(only=cfg.designs or None)
    corpus.save(ds, cfg.root)
    print(f"campaign {cfg.name}: {len(ds)} designs -> {cfg.root}")
    for d in ds:
        print(f"  {d['id']:28s} {d['family']:16s} {d['dcp_mb']:7.1f} MB")


def stage(name: str, cfg: Config, log) -> None:
    from .campaign import Campaign
    c = Campaign(cfg)
    t0 = time.time()
    log(f"=== {name} ===")
    if name == "space":
        from . import space
        spec = space.build_spec(cfg.vivado_version)
        space.save(spec, cfg.root / "space.json")
        log(f"space: {json.dumps(spec['size'])}")
    elif name == "probe":
        from . import probe
        probe.run(c, log=log)
    elif name == "profile":
        from . import profile
        profile.profile_all(cfg, c.designs(), log=log)
        for d in c.designs():
            c.features(d["id"])
    elif name == "baseline":
        from . import baseline
        baseline.run(c, log=log)
    elif name == "prune":
        from . import prune
        prune.initial(c, log=log)
    elif name == "search":
        from . import search
        search.run(c, log=log)
    elif name == "discover":
        from . import discover
        discover.run(c, log=log)
    elif name == "confirm":
        from . import confirm
        confirm.run(c, log=log)
    elif name == "distill":
        from . import distill
        distill.run(c, log=log)
    elif name == "compile":
        from . import compile as comp
        comp.run(c, log=log)
    elif name == "status":
        from . import status
        print(status.text(c))
        return
    log(f"=== {name} done in {time.time() - t0:.0f}s ===")


ALL = ["space", "profile", "probe", "baseline", "prune", "search", "discover", "confirm",
       "distill", "compile"]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="pramana")
    ap.add_argument("command")
    ap.add_argument("campaign")
    ap.add_argument("--designs", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--from", dest="from_stage", default=None)
    a = ap.parse_args(argv)
    if a.command == "init":
        return cmd_init(a)
    cfg = Config.load(a.campaign)
    log = log_to(cfg)
    if a.command == "all":
        names = ALL[ALL.index(a.from_stage):] if a.from_stage else ALL
        for n in names:
            stage(n, cfg, log)
    else:
        stage(a.command, cfg, log)


if __name__ == "__main__":
    sys.exit(main())
