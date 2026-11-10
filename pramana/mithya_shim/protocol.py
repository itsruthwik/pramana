"""Fixed run protocol of the mithya executor. Not learned; identical for every policy."""

RANGE_MARGIN = 0.1        # a feature may lie this fraction of the training span outside the
                          # training range before the design counts as out of range
DEFAULT_STEP_S = 120.0    # estimate per command when the cost model or design size is missing
STEP_LIMIT_S = 900        # a step estimated to take longer is not started
GRACE_S = 900             # the Vivado session is killed at budget + GRACE_S
OUT_OF_RANGE_BIN = "abstain"   # bin for a design outside the training range: it runs the
                               # policy's default_bin steps (chosen by pramana), every step as a
                               # candidate (keep-if-better)
