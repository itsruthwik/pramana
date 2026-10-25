"""Campaign state shared by all stages: designs, profiles, features, start states, results."""
from __future__ import annotations

import json
from pathlib import Path

from . import corpus
from .config import Config
from .features import from_profile
from .runner import Runner


class Campaign:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = cfg.root
        self.runner = Runner(cfg)

    # ---------------------------------------------------------------- designs
    def designs(self) -> list[dict]:
        ds = corpus.load(self.root)
        if self.cfg.designs:
            ds = [d for d in ds if d["id"] in self.cfg.designs]
        return ds

    def design(self, did: str) -> dict:
        return next(d for d in corpus.load(self.root) if d["id"] == did)

    def profile(self, did: str) -> dict:
        return json.loads((self.root / "profile" / did / "profile.json").read_text())

    def profile_dir(self, did: str) -> Path:
        return self.root / "profile" / did

    def features(self, did: str) -> dict:
        p = self.root / "profile" / did / "features.json"
        if not p.exists():
            p.write_text(json.dumps(from_profile(self.profile_dir(did)), indent=1))
        return json.loads(p.read_text())

    def spec(self) -> dict:
        return json.loads((self.root / "space.json").read_text())

    # ---------------------------------------------------------------- states
    def input_state(self, did: str) -> dict:
        d = self.design(did)
        return {"id": "input", "dcp": d["path"], "wns": self.profile(did)["input"]["wns"]}

    def results(self, did: str | None = None) -> list[dict]:
        rs = self.runner.load()
        return [r for r in rs if did is None or r["design"] == did]

    def best_state(self, did: str) -> dict:
        """Best legal state so far with a saved checkpoint (start point for the next depth)."""
        best = self.input_state(did)
        for r in self.results(did):
            if r["valid"] and r.get("dcp") and Path(r["dcp"]).exists() and \
                    r["result"]["wns"] > best["wns"]:
                best = {"id": r["run_id"], "dcp": r["dcp"], "wns": r["result"]["wns"],
                        "chain_key": r["chain_key"], "start": r["start"]}
        return best

    def state(self, did: str, sid: str) -> dict:
        if sid == "input":
            return self.input_state(did)
        r = next(r for r in self.results(did) if r["run_id"] == sid)
        return {"id": sid, "dcp": r["dcp"], "wns": r["result"]["wns"], "chain_key": r["chain_key"],
                "start": r["start"]}

    def lineage(self, did: str, sid: str) -> list[str]:
        """Chain keys from the input DCP to state sid."""
        out = []
        while sid != "input":
            st = self.state(did, sid)
            out.insert(0, st["chain_key"])
            sid = st["start"]
        return out

    def job(self, did: str, state: dict, chain: list[dict], purpose: str, **kw) -> dict:
        return {"design": did, "start": state["id"], "start_dcp": state["dcp"],
                "start_wns": state["wns"], "chain": chain, "purpose": purpose,
                "dcp_mb": self.design(did)["dcp_mb"], **kw}

    def noise_floor(self, did: str) -> float:
        """From repeated identical runs; at least cfg.noise_floor_ns."""
        groups: dict[tuple, list[float]] = {}
        for r in self.results(did):
            if r["valid"]:
                groups.setdefault((r["start"], r["chain_key"]), []).append(r["result"]["wns"])
        spreads = [max(v) - min(v) for v in groups.values() if len(v) > 1]
        return max([self.cfg.noise_floor_ns] + spreads)
