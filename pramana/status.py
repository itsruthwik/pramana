"""Campaign status summary."""
from __future__ import annotations

import json
from collections import Counter

from . import llm
from .campaign import Campaign


def text(c: Campaign) -> str:
    rs = c.results()
    L = [f"campaign {c.cfg.name}: {len(rs)} runs, "
         f"{sum(r['valid'] for r in rs)} valid; by purpose {dict(Counter(r['purpose'] for r in rs))}"]
    for d in c.designs():
        did = d["id"]
        try:
            inp = c.input_state(did)
        except Exception:
            L.append(f"  {did}: not profiled")
            continue
        b = c.best_state(did)
        n = sum(1 for r in rs if r["design"] == did)
        L.append(f"  {did:26s} {d['family']:14s} runs={n:3d} input={inp['wns']:+.3f} "
                 f"best={b['wns']:+.3f} (+{b['wns'] - inp['wns']:.3f})  "
                 f"{' > '.join(c.lineage(did, b['id']))[:110]}")
    sp = c.root / "search_state.json"
    if sp.exists():
        st = json.loads(sp.read_text())
        L.append(f"search: round {st['round']}, LLM weight {st['llm_weight']:.2f}, "
                 f"stopped {st['stopped']}")
    L.append(f"LLM spend: {json.dumps(llm.spend(c.cfg))}")
    return "\n".join(L)
