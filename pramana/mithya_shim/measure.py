"""design_features(profile_dir) -> the base features (features.py) of a design, from the
reports of profile.tcl. The selector also reads the reports themselves (profile.tcl and
profile_ext.tcl), so the extended profile is always needed."""
from pathlib import Path

import features


def design_features(profile_dir, ext_dir=None) -> dict:
    return features.from_profile(Path(profile_dir))
