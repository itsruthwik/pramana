"""Framework-level store: work that depends only on the DCP, part and Vivado version.

Done once and shared by every campaign (training or evaluation):
  specs/vivado_<ver>/<part>/space.json      probed search space   (committed)
  specs/vivado_<ver>/<part>/probe*.json     probe evidence         (committed)
  store/dcp/<id>-<md5>/vivado_<ver>/profile/  reports, paths, features (one dir per DCP)
  store/vivado_<ver>/t<threads>/results.jsonl reference (baseline) runs, records carry dcp_md5
Campaigns only own the work that depends on their choices (prune .. compile).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .config import REPO, Config

STORE = REPO / "store"


def dcp_key(d: dict) -> str:
    return f"{d['id']}-{d['md5'][:8]}"


def profile_dir(cfg: Config, d: dict) -> Path:
    return STORE / "dcp" / dcp_key(d) / f"vivado_{cfg.vivado_version}" / "profile"


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
