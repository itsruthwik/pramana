"""Run chains in Vivado and record results (the RUNNER and part of the SETTLER).

Each run: open a start state's checkpoint, run the expanded chain step by step, measure, dump
the top setup paths, write the checkpoint if it beats the start, and append one record to
results.jsonl. A run is identified by (design, start state, chain), so the same experiment is
never run twice (the case record).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import flow, tcl
from .config import Config
from .space import chain_key
from .vivado import est_mem_gb, run_vivado

_db_lock = threading.Lock()


def run_id(design: str, start: str, chain: list[dict], rep: int = 0, extra_tcl: str = "") -> str:
    key = f"{design}|{start}|{chain_key(chain)}"
    if rep:
        key += f"|rep{rep}"
    if extra_tcl:
        key += "|" + extra_tcl
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def build_script(start_dcp: str, chain: list[dict], run_dir: Path, threads: int,
                 keep_min_wns: float | None, extra_tcl: str = "") -> str:
    p = [tcl.header(threads), tcl.PATHS_DUMP, f"open_checkpoint {tcl.tcl_path(start_dcp)}"]
    for name, cmds, auto in flow.expand(chain):
        p.append(tcl.step_block(name + ("__auto" if auto else ""), cmds))
    if extra_tcl:
        p.append(tcl.step_block("custom", [extra_tcl]))
    p.append(tcl.FINISH_ROUTE)
    p.append("_emit_result")
    p.append(f"set _rd {tcl.tcl_path(run_dir)}")
    p.append(f"_paths_dump 50 [file join $_rd paths.tsv]")
    p.append(tcl.safe_report("timing_summary", "report_timing_summary -max_paths 10"))
    if keep_min_wns is not None:
        p.append(tcl.write_if_better(str(run_dir / "out.dcp"), keep_min_wns))
    p.append(f'puts "{tcl.DONE}"')
    return "\n".join(p) + "\n"


_STEP_RE = re.compile(r"^(CMD_OK|CMD_ERR) (\S+) secs=([\d.]+)(?: :: (.*))?$", re.M)
_SWNS_RE = re.compile(r"^STEP_WNS (\S+) wns=(\S+)$", re.M)


def parse_stdout(out: str, exit_code: int, timed_out: bool) -> dict:
    steps = {}
    for kind, name, secs, msg in _STEP_RE.findall(out):
        steps[name] = {"name": name, "ok": kind == "CMD_OK", "secs": float(secs),
                       "error": (msg or "")[:500] or None}
    for name, w in _SWNS_RE.findall(out):
        if name in steps:
            steps[name]["wns_after"] = None if w == "na" else float(w)
    result = None
    m = re.search(r"^RESULT (\{.*\})\s*$", out, re.M)
    if m:
        try:
            result = json.loads(m.group(1))
        except json.JSONDecodeError:
            result = None
    done = re.search(rf"^{tcl.DONE}\s*$", out, re.M) is not None
    if timed_out:
        status = "timeout"
    elif not done or result is None:
        status = "crash"
    elif any(not s["ok"] for s in steps.values()):
        status = "error"
    elif exit_code != 0:
        status = "crash"
    else:
        status = "ok"
    return {"status": status, "steps": list(steps.values()), "result": result,
            "wrote_dcp": "\nWROTE_DCP" in out}


def is_valid(result: dict | None) -> bool:
    """Admissible only if fully routed, 0 route errors, hold and pulse-width met."""
    if not result:
        return False
    try:
        return (result["routed"] == 1 and result["route_errors"] == 0
                and result["whs"] is not None and result["whs"] >= 0
                and result["wpws"] is not None and result["wpws"] >= 0
                and result["wns"] is not None and (result["routable_nets"] or 0) > 0)
    except (KeyError, TypeError):
        return False


class Runner:
    def __init__(self, cfg: Config, root: Path | None = None):
        self.cfg = cfg
        root = root or cfg.root
        self.db = root / "results.jsonl"
        self.runs_dir = root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ results db
    def load(self) -> list[dict]:
        if not self.db.exists():
            return []
        return [json.loads(ln) for ln in self.db.read_text().splitlines() if ln.strip()]

    def done_ids(self) -> set[str]:
        """Runs that finished (ok or a tool error). Crashes/timeouts are infrastructure
        failures (e.g. killed under memory pressure) and are retried."""
        own = {r["run_id"] for r in self.load() if r["status"] in ("ok", "error")}
        # runs already in another database (the framework store) are not repeated
        also = getattr(self, "also_done", None)
        return own | (also() if also else set())

    def _append(self, rec: dict) -> None:
        import fcntl
        with _db_lock:   # threads in this process; flock for other processes (trial runner)
            with self.db.open("a") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.write(json.dumps(rec) + "\n")
                f.flush()
                fcntl.flock(f, fcntl.LOCK_UN)

    # ------------------------------------------------------------------ one run
    def run(self, job: dict) -> dict:
        """job = {design, start, start_dcp, start_wns, chain, purpose, dcp_mb, [tag], [extra_tcl]}"""
        rid = run_id(job["design"], job["start"], job["chain"], job.get("rep", 0),
                     job.get("extra_tcl", ""))
        rdir = self.runs_dir / job["design"] / rid
        rdir.mkdir(parents=True, exist_ok=True)
        script = build_script(job["start_dcp"], job["chain"], rdir, job.get("threads", self.cfg.threads),
                              keep_min_wns=job.get("start_wns"), extra_tcl=job.get("extra_tcl", ""))
        (rdir / "run.tcl").write_text(script)
        t0 = time.time()
        vr = run_vivado(rdir / "run.tcl", rdir, self.cfg.vivado_settings, self.cfg.run_timeout_s,
                        need_gb=self.cfg.min_free_mem_gb + est_mem_gb(job.get("dcp_mb", 50)))
        (rdir / "stdout.txt").write_text(vr.stdout)
        parsed = parse_stdout(vr.stdout, vr.exit_code, vr.timed_out)
        res = parsed["result"]
        valid = parsed["status"] == "ok" and is_valid(res)
        d_wns = None
        if res and res.get("wns") is not None and job.get("start_wns") is not None:
            d_wns = round(res["wns"] - job["start_wns"], 4)
        opt_secs = sum(s["secs"] for s in parsed["steps"])
        rec = {
            "run_id": rid, "design": job["design"], "start": job["start"],
            "chain": job["chain"], "chain_key": chain_key(job["chain"]),
            "extra_tcl": job.get("extra_tcl"),
            "dcp_md5": job.get("dcp_md5"),
            "purpose": job.get("purpose", "?"), "tag": job.get("tag"), "round": job.get("round"),
            "rep": job.get("rep", 0),
            "status": parsed["status"], "valid": valid, "result": res,
            "start_wns": job.get("start_wns"), "d_wns": d_wns,
            "steps": parsed["steps"], "opt_secs": round(opt_secs, 1),
            "wall_s": round(vr.wall_s, 1), "dcp": str(rdir / "out.dcp") if parsed["wrote_dcp"] else None,
            "run_dir": str(rdir), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "threads": job.get("threads", self.cfg.threads),
        }
        if job.get("noise_of"):
            rec["noise_of"] = job["noise_of"]
        self._append(rec)
        return rec

    def run_many(self, jobs: list[dict], log=print) -> list[dict]:
        done = self.done_ids()
        todo = []
        for j in jobs:
            rid = run_id(j["design"], j["start"], j["chain"], j.get("rep", 0), j.get("extra_tcl", ""))
            if rid not in done:
                todo.append(j)
        log(f"[runner] {len(todo)} runs to do ({len(jobs) - len(todo)} already done), "
            f"{self.cfg.workers} workers")
        out = []
        with ThreadPoolExecutor(self.cfg.workers) as ex:
            futs = [ex.submit(self._safe_run, j) for j in todo]
            for fut in as_completed(futs):
                rec = fut.result()
                if rec:
                    log(f"[runner] {rec['design']:28s} {rec['status']:7s} valid={rec['valid']!s:5s} "
                        f"dWNS={rec['d_wns']} t={rec['wall_s']}s  {rec['chain_key'][:90]}")
                    out.append(rec)
        return out

    def _safe_run(self, job):
        try:
            rec = self.run(job)
            if rec["status"] == "crash" and not any(not st["ok"] for st in rec["steps"]):
                time.sleep(60)          # killed mid-run: wait for memory, retry once
                rec = self.run(job)
            return rec
        except Exception as e:  # a harness bug must not kill the pool
            print(f"[runner] HARNESS ERROR {job.get('design')}: {e!r}")
            return None
