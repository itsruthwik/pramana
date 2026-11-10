"""Fixed run protocol of the mithya executor. Not learned; identical for every policy."""

DEFAULT_STEP_S = 120.0    # estimate per command when the cost model or design size is missing
STEP_LIMIT_S = 900        # a step estimated to take longer is not started
GRACE_S = 900             # the Vivado session is killed at budget + GRACE_S
OUT_OF_RANGE_BIN = "abstain"   # when the selector fails or names no known policy: run the
                               # default policy (chosen by pramana), every step a candidate
