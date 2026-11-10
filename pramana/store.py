"""Framework-level store: work that depends only on the DCP, part and Vivado version.

Done once and shared by every campaign (training or evaluation):
  specs/vivado_<ver>/<part>/space.json      probed search space   (committed)
  specs/vivado_<ver>/<part>/probe*.json     probe evidence         (committed)
  store/dcp/<id>-<md5>/vivado_<ver>/input/    input check: part, md5, input metrics (S2 or S1)
  store/dcp/<id>-<md5>/vivado_<ver>/profile/  reports, paths, features (one dir per DCP)
  store/vivado_<ver>/t<threads>/results.jsonl reference (baseline) runs, records carry dcp_md5
Campaigns only own the work that depends on their choices (prune .. compile).
"""
from __future__ import annotations

import json
import shutil
import json
from pathlib import Path

from .config import REPO, Config

STORE = REPO / "store"


def dcp_key(d: dict) -> str:
    return f"{d['id']}-{d['md5'][:8]}"


def profile_dir(cfg: Config, d: dict) -> Path:
    return STORE / "dcp" / dcp_key(d) / f"vivado_{cfg.vivado_version}" / "profile"


def input_dir(cfg: Config, d: dict) -> Path:
    """Input check of one DCP (part, md5, input metrics); written by S2 or S1, see inputs.py."""
    return STORE / "dcp" / dcp_key(d) / f"vivado_{cfg.vivado_version}" / "input"


def reused_prepare_s(design_ids: list[str]) -> tuple[float, list[str]]:
    """Wall seconds of profile+baseline work imported from another store (scripts/
    import_store.py) that these designs reuse, and the sources. An import's wall time is
    shared out by each design's share of its Vivado seconds."""
    p = STORE / "imported.json"
    if not p.exists():
        return 0.0, []
    total, srcs = 0.0, []
    for imp in json.loads(p.read_text()).get("imports", []):
        vs = imp.get("vivado_s") or {}
        mine = sum(v for k, v in vs.items() if k in design_ids)
        if mine and sum(vs.values()):
            total += (imp.get("profile_wall_s", 0) + imp.get("baseline_wall_s", 0)) * mine / sum(vs.values())
            srcs.append(imp.get("source_log") or imp.get("source"))
    return total, srcs


def runs_root(cfg: Config) -> Path:
    return STORE / f"vivado_{cfg.vivado_version}" / f"t{cfg.threads}"


def spec_dir(cfg: Config) -> Path:
    return REPO / "specs" / f"vivado_{cfg.vivado_version}" / cfg.part


def shared_spec(cfg: Config) -> Path:
    return spec_dir(cfg) / "space.json"


def publish_spec(cfg: Config, campaign_root: Path) -> None:
    """Copy a fully probed campaign spec (and probe evidence) to the framework specs."""
    spec = json.loads((campaign_root / "space.json").read_text())
    if not (spec.get("probed") and spec.get("postroute_probed")):
        return
    d = spec_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    for f in ("space.json", "probe.json", "probe_postroute.json"):
        if (campaign_root / f).exists():
            shutil.copy2(campaign_root / f, d / f)
