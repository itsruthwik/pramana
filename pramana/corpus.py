"""Register training DCPs: id, path, hash, size, family.

Families matter because every "does this hold on unseen designs" check holds out whole
families. Families here are provisional (inferred from benchmark source) until the user
confirms them; a campaign mixing parts is rejected later by the profiler.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

# FPL = Path("/mnt/vault0/rsunketa/FPL_contest")
# DEFAULT_SOURCES = [FPL / "data/fpl26_contest_benchmarks", FPL / "new-dcp/dcp"]
DEFAULT_SOURCES = os.environ.get(
    "PRAMANA_SOURCES",
    str(Path(__file__).resolve().parent.parent / "fpl26_contest_benchmarks")).split(":")
# Provisional family labels by benchmark origin.
FAMILY_RULES = [
    (r"^rosetta_", "rosetta_hls"),
    (r"^(vexriscv|boom_soc|arm_core)", "cpu"),
    (r"^(vtr_mcml|bgm|LU32PEEng|LU64PEEng)", "vtr"),
    (r"^(bnn|dla_like|lenet|robot_rl)", "koios_ml"),
    (r"^(finn_|logicnets_)", "nn_dataflow"),
    (r"^corescore", "manycore_soc"),
    (r"^amd_mini-isp", "vision_ip"),
    (r"^ispd16", "ispd_placement"),
    (r"^fir_", "dsp_systolic"),
]
SKIP = re.compile(r"_optimized-|_v2")


def design_id(path: Path) -> str:
    return re.sub(r"_2025\.1$", "", path.stem)


def family_of(did: str) -> str:
    for pat, fam in FAMILY_RULES:
        if re.match(pat, did):
            return fam
    return "unknown"


def md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def register(sources: list[Path] | None = None, only: list[str] | None = None) -> list[dict]:
    out = []
    for src in sources or DEFAULT_SOURCES:
        for p in sorted(Path(src).glob("*.dcp")):
            if SKIP.search(p.name):
                continue
            did = design_id(p)
            if only and did not in only:
                continue
            out.append({"id": did, "path": str(p), "md5": md5(p),
                        "dcp_mb": round(p.stat().st_size / 1e6, 1), "family": family_of(did)})
    return out


def register_list(csv_path: Path) -> list[dict]:
    """Register exactly the DCPs listed in a CSV (columns `path`, optional `source`), e.g.
    dataset/split_train.csv. No name-based skipping (a listed `_v2` DCP is kept). Family: the
    FAMILY_RULES match if any, else the benchmark source (PACT, Koios 2.0, VTR, ...), so related
    designs from one suite share a family for the leave-one-family-out checks."""
    import csv
    base = Path(csv_path).resolve().parent.parent      # paths in the CSV are repo-relative
    out = []
    for row in csv.DictReader(open(csv_path)):
        p = Path(row["path"])
        p = p if p.is_absolute() else (base / p)
        did = design_id(p)
        fam = family_of(did)
        if fam == "unknown" and row.get("source"):
            fam = re.sub(r"[^a-z0-9]+", "_", row["source"].lower()).strip("_")
        out.append({"id": did, "path": str(p.resolve()), "md5": md5(p),
                    "dcp_mb": round(p.stat().st_size / 1e6, 1), "family": fam})
    ids = [d["id"] for d in out]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"duplicate design ids in {csv_path}: {sorted(dup)}")
    return out


def save(designs: list[dict], root: Path) -> None:
    (root / "designs.json").write_text(json.dumps(designs, indent=1))


def load(root: Path) -> list[dict]:
    return json.loads((root / "designs.json").read_text())
