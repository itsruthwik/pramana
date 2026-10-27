"""Adapters to LATCH / PACT knowledge formats.  Run: python3 -m pytest tests -q"""
import json
from pathlib import Path

import pytest

from pramana import adapters
from pramana.compile import bins

KB = Path(__file__).resolve().parents[1] / "campaigns" / "dev2" / "kb" / "kb.json"
pytestmark = pytest.mark.skipif(not KB.exists(), reason="dev2 kb.json not present")


def _write(tmp_path, name):
    kb = json.loads(KB.read_text())
    return kb, adapters.write_all(kb, bins(kb), tmp_path / name)


def test_files_exist_and_deterministic(tmp_path):
    _, a = _write(tmp_path, "a")
    _, b = _write(tmp_path, "b")
    assert set(a) == set(b)
    for want in ("latch/reference_manual_pramana.json", "latch/recipe_specs.json", "latch/README.md",
                 "pact/recipe_library.json", "pact/prior_cases.jsonl", "pact/README.md"):
        assert want in a and a[want].stat().st_size > 0
    for k in a:
        assert a[k].read_bytes() == b[k].read_bytes(), k


def test_latch_manual_matches_loader_contract(tmp_path):
    """Checks LATCH's loader contract (flow_cards keyed by the 9 allowed flows, required card
    fields, the compactor's 900-char / 20-item limits), restated from LATCH's source."""
    _, f = _write(tmp_path, "o")
    man = json.loads(f["latch/reference_manual_pramana.json"].read_text())
    cards = man["flow_cards"]
    assert set(cards) == set(adapters.LATCH_FLOWS)
    for fid, c in cards.items():
        assert c["id"] == fid and all(k in c for k in adapters.LATCH_CARD_FIELDS)
        strings = [c["description"], c["ceiling_note"], *c["use_when"], *c["avoid_when"]]
        assert all(len(s) <= adapters.LATCH_MAX_STR for s in strings)
        assert len(c["use_when"]) <= adapters.LATCH_MAX_LIST >= len(c["avoid_when"])
    assert any("[same] phys_opt:directive=Explore" in s for s in cards["phys_opt"]["use_when"])


def test_latch_recipes_use_only_allowlisted_primitives(tmp_path):
    _, f = _write(tmp_path, "o")
    specs = json.loads(f["latch/recipe_specs.json"].read_text())
    allowed = {p: set(d) for p, d in adapters.LATCH_PRIMS.values()}
    for r in specs["recipes"]:
        assert 1 <= len(r["steps"]) <= 8
        for s in r["steps"]:
            if s["primitive"].startswith("macro."):
                assert s["primitive"] in adapters.LATCH_MACROS.values() and s["args"] == {}
            else:
                assert s["args"]["directive"] in allowed[s["primitive"]]


def test_pact_numbers_come_from_kb(tmp_path):
    kb, f = _write(tmp_path, "o")
    lib = json.loads(f["pact/recipe_library.json"].read_text())
    acts = {a["factor"]: a for a in kb["actions"]}
    assert set(lib["families"]) == set(adapters.PACT_FAMILIES) and len(lib["families"]) == 9
    for r in lib["recipes"]:
        fac = r["provenance"].get("pramana_factor")
        if fac:
            assert r["prior"]["median_d_wns"] == acts[fac]["profile"]["median_d_wns"]
    for line in f["pact/prior_cases.jsonl"].read_text().splitlines():
        c = json.loads(line)
        fac = c["action_ref"].removeprefix("pramana:")
        if fac in acts:
            assert acts[fac]["per_design"][c["design"]] == c["d_wns_ns"]
