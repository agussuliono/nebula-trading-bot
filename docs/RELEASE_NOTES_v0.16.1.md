# NEBULA v0.16.1

## Fixed

- `nebula --help` raised `TypeError: %o format: an integer is required, not dict` because the help string for `--sensitivity` contained a literal percent sign. `argparse` applies old-style percent interpolation to help strings. The wording now uses “percent”.
- Added a CLI regression test to ensure top-level help exits successfully and displays the sensitivity description.

## Explicitly unchanged

- No strategy parameters or trading rules were changed.
- No OKX API, credentials, order, cancel, or account behavior was changed.
- `NEBULA_ENABLE_DEMO_ORDERS` remains disabled by default.

## Validation

- The v0.16.0 baseline suite passed 190 tests before the patch.
- v0.16.1 test and CLI-help results are recorded in the build report accompanying this release.
- Historical strategy results remain negative and do not qualify NEBULA for the 14-day Demo forward test.
