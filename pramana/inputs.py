"""Input check: what every stage that runs from the input DCP needs to know about it.

One short read-only Vivado session per DCP: open the checkpoint, print its part and SLR count,
emit the RESULT block (WNS, WHS, WPWS, routed state, clocks, ...). Python adds the md5 check.
The record is cached in the framework store, beside (not inside) the S1 profile:

  store/dcp/<id>-<md5>/vivado_<ver>/input/input.json

S2 baseline runs this itself, so it does not depend on S1 profile. S1 writes the same record
from its own measurement, so running S1 first costs nothing extra. Campaign.input_state()
reads the start WNS from here.

Designs on another part than the campaign's, or whose DCP changed since `pramana init`, are
excluded: recorded in <campaign>/excluded.json and dropped from campaign.json `designs`.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import tcl
from .config import Config
from .corpus import md5
from .runner import parse_stdout
from .store import input_dir
from .vivado import est_mem_gb, run_vivado

PART_RE = re.compile(r"^PART (\S+)", re.M)
NSLR_RE = re.compile(r"^N_SLR (\d+)", re.M)


def script(dcp: str, threads: int) -> str:
    return "\n".join([tcl.header(threads), f"open_checkpoint {tcl.tcl_path(dcp)}",
                      'puts "PART [get_property PART [current_design]]"',
                      'puts "N_SLR [llength [get_slrs -quiet]]"',
                      "_emit_result", f'puts "{tcl.DONE}"']) + "\n"


def record(d: dict, stdout: str, exit_code: int, timed_out: bool, secs: float,
           source: str) -> dict:
    """Build the input record from a Vivado session's stdout (shared with S1 profile)."""
    parsed = parse_stdout(stdout, exit_code, timed_out)
    part, nslr = PART_RE.search(stdout), NSLR_RE.search(stdout)
    return {"id": d["id"], "md5": d["md5"], "status": parsed["status"],
            "input": parsed["result"],
            "part": part.group(1) if part else None,
            "n_slr": int(nslr.group(1)) if nslr else None,
            "dcp_unchanged": md5(Path(d["path"])) == d["md5"],
            "secs": round(secs, 1), "source": source}


def path(cfg: Config, d: dict) -> Path:
    return input_dir(cfg, d) / "input.json"


def load(cfg: Config, d: dict) -> dict | None:
    p = path(cfg, d)
    return json.loads(p.read_text()) if p.exists() else None


def save(cfg: Config, d: dict, rec: dict) -> None:
    """Keep a successful record only: a failed check is retried next time."""
    if rec["status"] != "ok" or not rec.get("input"):
        return
    p = path(cfg, d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, indent=1))


def check(cfg: Config, d: dict) -> dict:
    rec = load(cfg, d)
    if rec is not None:
        return rec
    out = input_dir(cfg, d)
    out.mkdir(parents=True, exist_ok=True)
    (out / "input.tcl").write_text(script(d["path"], cfg.threads))
    vr = run_vivado(out / "input.tcl", out, cfg.vivado_settings, cfg.run_timeout_s,
                    need_gb=cfg.min_free_mem_gb + est_mem_gb(d["dcp_mb"]))
    (out / "stdout.txt").write_text(vr.stdout)
    rec = record(d, vr.stdout, vr.exit_code, vr.timed_out, vr.wall_s, "input_check")
    save(cfg, d, rec)
    return rec


def reasons(cfg: Config, rec: dict) -> str | None:
    """Why a design cannot be used from its input DCP, or None if it can."""
    if rec["status"] != "ok" or not rec.get("input"):
        return f"input check failed ({rec['status']})"
    if rec["part"] and rec["part"] != cfg.part:
        return f"part {rec['part']}"
    if not rec["dcp_unchanged"]:
        return "DCP changed since init (md5 differs)"
    if (rec["input"] or {}).get("wns") is None:
        return "no setup path in the input"
    return None


def exclude(cfg: Config, recs: list[dict], log=print, stage: str = "input") -> list[dict]:
    """Drop designs that fail reasons(); record why. Returns the usable records."""
    bad = {r["id"]: why for r in recs if (why := reasons(cfg, r))}
    for did, why in bad.items():
        log(f"[{stage}] EXCLUDED {did}: {why}")
    if bad:
        ids = cfg.designs or [r["id"] for r in recs]
        cfg.designs = [i for i in ids if i not in bad]
        cfg.save()
        f = cfg.root / "excluded.json"
        old = json.loads(f.read_text()) if f.exists() else {}
        f.write_text(json.dumps({**old, **bad}, indent=1))
    return [r for r in recs if r["id"] not in bad]


def check_all(cfg: Config, designs: list[dict], log=print) -> list[dict]:
    """Check every design's input DCP (cached), exclude unusable ones, return the rest."""
    todo = [d for d in designs if load(cfg, d) is None]
    if todo:
        log(f"[input] checking {len(todo)} input DCP(s) ({len(designs) - len(todo)} cached)")
    with ThreadPoolExecutor(max(1, cfg.workers)) as ex:
        recs = list(ex.map(lambda d: check(cfg, d), designs))
    for r in recs:
        inp = r.get("input") or {}
        if r["source"] == "input_check" and r["id"] in {d["id"] for d in todo}:
            log(f"[input] {r['id']:28s} {r['status']} part={r['part']} wns={inp.get('wns')} "
                f"whs={inp.get('whs')} unchanged={r['dcp_unchanged']} t={r['secs']}s")
    return exclude(cfg, recs, log, "input")
