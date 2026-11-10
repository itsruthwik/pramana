"""Loads the learned policy (policy.json, written by pramana) next to this file."""
import json
from pathlib import Path

POLICY = json.loads((Path(__file__).resolve().parent / "policy.json").read_text())
