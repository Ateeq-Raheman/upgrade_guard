# Contributing rules
1. Add the item to the right `rules/vA_to_vB.yaml` (ID prefix `V<to>-NNN`).
2. Prefer AST kinds (`py_name`, `py_call_kw`, …) over `regex` — fewer false positives.
3. Add a test case in `tests/test_engine.py` showing one hit and one non-hit.
4. If it can be rewritten mechanically, add `autofix` and mark `safe: false` unless the rewrite is always correct.
5. Link the upstream PR/commit in `link`.
