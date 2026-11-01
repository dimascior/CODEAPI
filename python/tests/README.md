# `python/tests/`

Test suite for the CODEAPI Python port. **Phase 10** moves the proven suites here
from `legacy-seed/cli/`:

- `test_smoke.py` — offline compile/help checks + live-window suite driver.
- `test_differential.py` — proves each Python wrapper matches raw `/exec`
  (calls both paths, diffs output). This is the safety net for Phases 10–14:
  no old path is retired until the Python path is differential-equivalent.

Until Phase 10, the working suites live at
[`../../legacy-seed/cli/test_smoke.py`](../../legacy-seed/cli/test_smoke.py)
and
[`../../legacy-seed/cli/test_differential.py`](../../legacy-seed/cli/test_differential.py)
(git-ignored under `/legacy-seed/`, which is exactly why they must move
here to reach `main`).
