"""Campaign configuration: paths, tool versions, resource limits.

One Config object is created per campaign and saved as campaign.json so every stage of a
campaign runs with the same settings.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

VIVADO_SETTINGS = "/mnt/vault0/tools/Vitis_2025.2/2025.2/Vivado/settings64.sh"
VIVADO_VERSION = "2025.2"


@dataclass
class Config:
    name: str
    part: str = "xcvu3p-ffvc1517-2-e"
    vivado_settings: str = VIVADO_SETTINGS
    vivado_version: str = VIVADO_VERSION
    threads: int = 4                  # fixed per run so run times are comparable
    workers: int = 4                  # parallel Vivado processes
    min_free_mem_gb: float = 16.0      # never launch below this much free memory
    run_timeout_s: float = 4 * 3600
    # LLM (Claude Code headless, user's subscription)
    llm_model: str = "claude-sonnet-5-5"
    llm_effort: str = "low"
    # search budget
    rounds: int = 4
    runs_per_design_per_round: int = 8
    share_exploit: float = 0.7
    share_test: float = 0.2
    share_audit: float = 0.1
    noise_floor_ns: float = 0.005     # minimum; raised by measured repeat spread
    discover_trials: int = 6
    designs: list[str] = field(default_factory=list)   # restrict to these ids (empty = all)

    @property
    def root(self) -> Path:
        return REPO / "campaigns" / self.name

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "campaign.json").write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, name: str) -> "Config":
        p = REPO / "campaigns" / name / "campaign.json"
        return cls(**json.loads(p.read_text()))
