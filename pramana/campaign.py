"""Campaign state shared by all stages: designs, profiles, features, start states, results."""
from __future__ import annotations

import json
from pathlib import Path

from . import corpus, store
from .config import Config
from .features import from_profile
from .runner import Runner


class Campaign:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = cfg.root
        self.runner = Runner(cfg)
        # reference runs live in the framework store, shared by all campaigns
        self.store_runner = Runner(cfg, store.runs_root(cfg))
        # a campaign run identical to a reference run (same design, start, chain) is not
        # re-run; results() returns the reference record for it
        self.runner.also_done = self.store_runner.done_ids

    # ---------------------------------------------------------------- designs
    def designs(self) -> list[dict]:
        ds = corpus.load(self.root)
        if self.cfg.designs:
            ds = [d for d in ds if d["id"] in self.cfg.designs]
        return ds

    def design(self, did: str) -> dict:
        return next(d for d in corpus.load(self.root) if d["id"] == did)

    def profile(self, did: str) -> dict:
        return json.loads((self.profile_dir(did) / "profile.json").read_text())

    def profile_dir(self, did: str) -> Path:
        p = store.profile_dir(self.cfg, self.design(did))
        old = self.root / "profile" / did          # campaigns made before the store
        return old if not p.exists() and old.exists() else p

    def features(self, did: str) -> dict:
        p = self.profile_dir(did) / "features.json"
        if not p.exists():
            p.write_text(json.dumps(from_profile(self.profile_dir(did)), indent=1))
        return json.loads(p.read_text())

    def spec(self) -> dict:
        return json.loads((self.root / "space.json").read_text())

    # ---------------------------------------------------------------- states
    def input_state(self, did: str) -> dict:
        d = self.design(did)
        return {"id": "input", "dcp": d["path"], "wns": self.profile(did)["input"]["wns"]}

    def results(self, did: str | None = None, include_noise: bool = False) -> list[dict]:
        """This campaign's runs plus the store's reference runs for its DCPs (same md5).
        Noise-estimate runs (perturbed constraints) are left out unless asked for."""
        md5 = {d["id"]: d["md5"] for d in self.designs()}
        own = self.runner.load()
        shared = [r for r in self.store_runner.load() if md5.get(r["design"]) == r.get("dcp_md5")]
        if shared:
            # a run id can appear in both (campaigns made before the store, or a search run
            # identical to a reference run); keep the record with the most final status;
            # on ties keep the reference record (its tag identifies it as a control)
            rank = {"ok": 3, "error": 2}
            ids = {r["run_id"]: i for i, r in enumerate(own)}
            for r in shared:
                i = ids.get(r["run_id"])
                if i is None:
                    ids[r["run_id"]] = len(own)
                    own.append(r)
                elif rank.get(r["status"], 0) > rank.get(own[i]["status"], 0) or (
                        rank.get(r["status"], 0) == rank.get(own[i]["status"], 0)
                        and r["purpose"] in ("baseline", "noise")):
                    own[i] = r
        return [r for r in own if (r["design"] == did if did else r["design"] in md5)
                and (include_noise or r["purpose"] != "noise")]

    def best_state(self, did: str) -> dict:
        """Best legal state so far with a saved checkpoint (start point for the next depth)."""
        best = self.input_state(did)
        for r in self.results(did):
            if r["valid"] and r.get("dcp") and Path(r["dcp"]).exists() and \
                    r["result"]["wns"] > best["wns"]:
                best = {"id": r["run_id"], "dcp": r["dcp"], "wns": r["result"]["wns"],
                        "chain_key": r["chain_key"], "start": r["start"]}
        return best

    def top_states(self, did: str, k: int = 2) -> list[dict]:
        """Best k legal states with saved checkpoints and distinct WNS (beam for the search)."""
        cands = [self.input_state(did)]
        for r in self.results(did):
            if r["valid"] and r.get("dcp") and Path(r["dcp"]).exists():
                cands.append({"id": r["run_id"], "dcp": r["dcp"], "wns": r["result"]["wns"],
                              "chain_key": r["chain_key"], "start": r["start"]})
        cands.sort(key=lambda s: -s["wns"])
        out, seen = [], set()
        for s in cands:
            if round(s["wns"], 3) in seen:
                continue
            seen.add(round(s["wns"], 3))
            out.append(s)
            if len(out) == k:
                break
        return out

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
            r = next(r for r in self.results(did) if r["run_id"] == sid)
            label = r["chain_key"]
            if r.get("extra_tcl"):
                label = (label + " + " if label else "") + f"custom Tcl recipe ({r.get('tag')})"
            out.insert(0, label)
            sid = r["start"]
        return out

    def job(self, did: str, state: dict, chain: list[dict], purpose: str, **kw) -> dict:
        d = self.design(did)
        return {"design": did, "start": state["id"], "start_dcp": state["dcp"],
                "start_wns": state["wns"], "chain": chain, "purpose": purpose,
                "dcp_mb": d["dcp_mb"], "dcp_md5": d["md5"], **kw}

    def noise_floor(self, did: str) -> float:
        """Run-to-run sensitivity of the result; at least cfg.noise_floor_ns.

        Vivado is deterministic, so identical repeats give no spread. The estimate comes from
        reference chains re-run with a tiny temporary clock tightening (baseline.NOISE): the
        result moves only because the tool's trajectory changed. Per chain, half the range of
        {reference, perturbed runs}; the design's floor is the largest of those."""
        rs = self.results(did, include_noise=True)
        key = (did, len(rs))
        cache = self.__dict__.setdefault("_noise_cache", {})
        if key in cache:
            return cache[key]
        groups: dict[tuple, list[float]] = {}
        ref = {r["tag"]: r for r in rs if r["purpose"] == "baseline" and r["valid"]}
        for r in rs:
            if r["purpose"] == "noise" and r["valid"] and r.get("noise_of") in ref:
                groups.setdefault(("noise", r["noise_of"]), [ref[r["noise_of"]]["result"]["wns"]]) \
                    .append(r["result"]["wns"])
            elif r["valid"] and r["purpose"] != "noise":
                # the recipe is part of the key: different recipes on the same (empty) chain
                # are different runs, not repeats
                groups.setdefault((r["start"], r["chain_key"], r.get("extra_tcl") or ""),
                                  []).append(r["result"]["wns"])
        spreads = [(max(v) - min(v)) / (2 if k[0] == "noise" else 1)
                   for k, v in groups.items() if len(v) > 1]
        cache[key] = round(max([self.cfg.noise_floor_ns] + spreads), 4)
        return cache[key]
