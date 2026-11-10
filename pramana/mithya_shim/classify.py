"""choose_policy(profile_dir, ext_dir, features) -> policy name, by the selector pramana learned
(policy.json "selector": its code file and fitted model). If the selector fails or names no
known policy, the design gets OUT_OF_RANGE_BIN: the default policy, every step a candidate."""
from pathlib import Path

import selector as _sel
from policy import POLICY
from protocol import OUT_OF_RANGE_BIN

HERE = Path(__file__).resolve().parent


def choose_policy(profile_dir, ext_dir, features):
    s = POLICY["selector"]
    return _sel.choose(HERE / s["file"], s["model"], profile_dir, ext_dir, features,
                       POLICY["policies"], OUT_OF_RANGE_BIN)
