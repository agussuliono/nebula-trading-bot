# GitHub OKX Demo credential handling

## Purpose

The `okx-demo-readonly` GitHub Environment is reserved for a **Demo API key with Read permission only**. The `OKX_API_KEY`, `OKX_SECRET_KEY`, and `OKX_PASSPHRASE` Environment secrets are consumed only by the manually dispatched `OKX Demo read-only verification` workflow.

The workflow:
- runs only when dispatched against `main`;
- uses the `okx-demo-readonly` Environment;
- explicitly sets `NEBULA_ENABLE_DEMO_ORDERS=false` and `NEBULA_KILL_SWITCH=true`;
- explicitly locks `NEBULA_OKX_REGION=global`;
- calls `nebula verify-demo-once`, which performs public and authenticated private GET requests only;
- does not grant order submission, cancellation, transfer, or withdrawal capability.

## Required permissions

For this read-only Environment, the API key must have exactly the OKX `Read` permission. The verification code rejects `Trade`, `Withdraw`, `Transfer`, `Loan`, `Earn`, `Onchain`, and unknown permission scopes.

Do not put a Trade-enabled key in this Environment. A future Demo execution test, if separately authorized, needs a distinct Demo key and a separately reviewed execution configuration. It must never reuse this read-only workflow or enable order submission as a side effect of connectivity verification.

## Secret handling

- Store all three values only as Environment secrets under `okx-demo-readonly`.
- Never put credentials in source files, commits, issues, pull-request text, workflow output, or chat.
- Do not print environment variables or request secret values for troubleshooting.
- If a credential was ever committed or exposed, revoke it at OKX and create a replacement.
- Keep Withdraw permission disabled on every NEBULA key.

## Region

The workflow explicitly sets `NEBULA_OKX_REGION=global`, matching the account-registration region confirmed for this project. US/AU and EEA accounts require their own documented API domains; do not change the region based on physical location.

## Verification boundary

A successful CI unit-test run proves only offline behavior. The read-only workflow is a separate, manually initiated account check and has not been run by this change. It must not be started automatically during pull-request validation.
