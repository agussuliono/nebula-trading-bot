# NEBULA v0.16.6 — fail-closed emergency stop default

## Change

- The emergency stop now defaults to ON in both direct Settings construction and environment loading.
- Clearing the emergency stop requires an explicit `NEBULA_KILL_SWITCH=false` setting.
- Demo order submission remains disabled by default through `NEBULA_ENABLE_DEMO_ORDERS=false`.
- Added regression tests for both safety defaults.
- Added weekly Dependabot checks for Python dependencies and GitHub Actions versions.

## Security rationale

The previous default left the emergency stop inactive unless the environment explicitly set it. Although the Demo order gate was already OFF by default, an inactive emergency stop was weaker than the project's fail-closed safety policy.

## Verification

GitHub Actions runs the complete test suite, compile checks, CLI check, and safety-default assertions after these commits.
