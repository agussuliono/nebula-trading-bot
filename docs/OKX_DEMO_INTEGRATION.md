# OKX Demo API integration check

NEBULA uses the OKX V5 REST API with a fixed Demo marker. The official OKX
documentation specifies the shared REST host and requires
`x-simulated-trading: 1` on Demo Trading requests.

Official reference: https://www.okx.com/docs-v5/en/

## What this verification does

Run:

```bash
nebula verify-demo-once
```

The command performs only these reads:

- Public Spot instruments and tickers.
- Authenticated account configuration.
- Trading-account balances.
- Pending regular Spot orders.
- Pending OCO and conditional algo orders.

It checks that the fixed REST host and Demo header are in use, the account is
in Spot mode, auto-loan and Spot borrowing are disabled, and the API key has
Read permission without Withdraw permission. Trade permission is neither
required nor used by this read-only verification. It reports whether Trade
permission is present so operators can identify an over-privileged key.
It also reports open orders and non-USDT holdings as blockers for a clean
initial integration.

Pending regular and algo order lists are read with the documented 100-record
page size and `after` cursors until the complete snapshot is obtained.
Repeated/missing cursors or a page-limit overflow fail closed rather than
treating a partial list as an empty account. Balance amounts are validated
with exact decimal arithmetic.

It never submits, cancels, amends, transfers, or adjusts funds. It does not
print API keys, secrets, passphrases, account UIDs, or balance amounts.

## Prepare a dedicated read-only Demo API key for GitHub Actions

1. Log in to OKX Demo Trading and open Personal Center → Demo Trading API.
2. Create a separate key for NEBULA connectivity checks.
3. Grant **Read only**. Do not grant Trade, Withdraw, Transfer, Loan, Earn,
   or Onchain permissions.
4. Do not reuse a key that has Trade permission for this GitHub Actions
   verification job.
5. Store the three credentials as GitHub Environment secrets named
   `OKX_API_KEY`, `OKX_SECRET_KEY`, and `OKX_PASSPHRASE` in the
   `okx-demo-readonly` environment. Never put them in workflow YAML,
   repository files, issues, pull-request comments, or ChatGPT.
6. In the GitHub Environment settings, set deployment branches to
   **Selected branches only** and allow only `main`. This is an independent
   protection: a workflow-level branch condition alone is not a security
   boundary because workflow code can be changed on another branch.
7. Optionally require a trusted reviewer before deployments if the account
   or repository has additional writers. Review workflow changes before
   adding or updating secrets.

GitHub-hosted runner IP addresses are not fixed by default. Do not assume a
single IP allowlist will work for GitHub-hosted runners. OKX recommends IP
binding where a stable runner IP is available; do not weaken the workflow or
reuse a more privileged key merely to work around IP restrictions.

## Run the GitHub Actions verification

The workflow is `.github/workflows/okx-demo-verify.yml`. It is manually
triggered, has read-only repository permissions, uses the named GitHub
Environment, and explicitly sets order submission off and the emergency stop
on. It runs only `nebula verify-demo-once`; it does not run the orchestrator.

After this workflow file is present on the repository's default branch,
open GitHub → Actions → **OKX Demo read-only verification** → Run workflow.
Choose the default branch and start the run. A `workflow_dispatch` workflow
must exist on the default branch to be manually triggerable.

## Interpreting the result

- `read_only_demo_connection_verified`: public and authenticated reads passed
  and the checked account properties passed. This is not approval to trade.
- `connected_but_not_ready`: the API connected, but one or more account
  conditions need manual review. Do not enable order submission.
- `blocked`: credentials, endpoint checks, or API reads failed. No order
  write is attempted.

If the report shows existing orders or non-USDT holdings, do not delete or
convert them just to clear the check. First identify whether they belong to
the dedicated NEBULA Demo account. Any balance adjustment or order action
requires separate explicit approval.

## Automated contract tests

CI uses deterministic fake responses. It verifies the Demo header, REST host,
authentication signature, documented query parameters, Spot cash OCO payload,
error handling, and zero write calls in the verification workflow. These are
offline protocol tests, not proof of a successful connection to OKX.

## Not yet performed

No real Demo API key has been supplied to or accessed by this development
task. The read-only command has not been run against the user's account. No
live or Demo order has been submitted. The 14-day forward test has not started.
