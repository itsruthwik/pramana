# mithya (shim)

A fixed executor for a pramana-trained policy. It holds no knowledge about Vivado or designs:

- `policy.json` (written by pramana's compile stage) holds everything learned: the bin rule
  (feature and threshold), the training feature ranges, the per-bin step lists with their
  order, Tcl and mode (committed / candidate), the extra actions, the budget-dependent
  policies and the runtime cost model.
- `protocol.py` holds the fixed run protocol: range margin, default step estimate, step limit,
  grace period and the out-of-range rule. These are the same for every policy and are not
  learned.
- `classify.py`, `recipes.py`, `executor.py`, `ladder.py` interpret `policy.json` under that
  protocol. `kib.tcl` is the keep-if-better Tcl. `features.py` and `profile.tcl` measure a design.

These files are the same for every trained bundle; only `policy.json` changes.
