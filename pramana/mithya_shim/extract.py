"""load_reports(profile_dir, ext_dir) -> {report name: text}, the input a strategy selector
reads. Fixed code: pramana's training and mithya use this same file, so a selector sees the
same reports in both.

Names: S1 profile files as '<name>' (e.g. 'utilization', 'paths'), extended profile files as
'ext/<name>' (e.g. 'ext/utilization_hier', 'ext/hold_paths'). A report Vivado could not
produce starts with 'REPORT_ERROR'.
"""
from pathlib import Path


def load_reports(profile_dir, ext_dir=None) -> dict:
    out = {}
    for base, pre in ((profile_dir, ""), (ext_dir, "ext/")):
        if base and Path(base).exists():
            for f in sorted(Path(base).iterdir()):
                if f.suffix in (".txt", ".tsv") and f.stem != "stdout":
                    out[pre + f.stem] = f.read_text(errors="replace")
    return out
