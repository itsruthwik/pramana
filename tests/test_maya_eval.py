"""maya_eval result parsing (no Vivado, no LLM)."""
import json
import tempfile
from pathlib import Path

from pramana.maya_eval import parse_run


def test_parse_run_banked_and_tokens():
    d = Path(tempfile.mkdtemp())
    (d / "token_usage.json").write_text(json.dumps({"summary": {
        "initial_wns": -1.0, "best_wns": -0.4, "total_llm_calls": 3, "total_tokens": 900,
        "total_prompt_tokens": 800, "total_completion_tokens": 100, "total_cost": 0.01}}))
    rows = [{"accepted": True, "gate_ok": True, "wns": -0.6},
            {"accepted": False, "gate_ok": False, "wns": -0.2},   # not banked: ignored
            {"accepted": True, "gate_ok": True, "wns": -0.4}]
    (d / "attempts.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    r = parse_run(d)
    assert r["final_wns"] == -0.4 and r["d_wns"] == 0.6 and r["valid"]
    assert r["attempts"] == 3 and r["banked"] == 2 and r["total_tokens"] == 900


def test_parse_run_nothing_banked_ships_input():
    d = Path(tempfile.mkdtemp())
    (d / "token_usage.json").write_text(json.dumps({"summary": {"initial_wns": -1.0}}))
    r = parse_run(d)
    assert r["final_wns"] == -1.0 and r["d_wns"] == 0.0 and r["valid"]
    assert not parse_run(Path(tempfile.mkdtemp()))["valid"]     # maya never finished
