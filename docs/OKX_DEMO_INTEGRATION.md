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
both Read and Trade permissions but no Withdraw permission. It reports
non-USDT balances and outstanding orders as blockers for a clean initial
integration.

It never submits, cancels, amends, transfers, or adjusts funds. It does not
print API keys, secrets, passphrases, account UIDs, or balance amounts.

## Prepare a Demo API key locally

1. Log in to OKX and open Trade → Demo Trading → Personal Center → Demo Trading
   API. Create a Demo Trading API key.
2. Grant Read and Trade only. Do not grant Withdraw.
3. Store the credentials locally as environment variables. Do not paste them
   into ChatGPT, commit them to Git, or put them in a config file.

Linux/macOS shell example:

```bash
export OKX_API_KEY='YOUR_DEMO_API_KEY'
export OKX_SECRET_KEY='YOUR_DEMO_SECRET'
export OKX_PASSPHRASE='YOUR_DEMO_PASSPHRASE'
nebula verify-demo-once
```

PowerShell example:

```powershell
$env:OKX_API_KEY = 'YOUR_DEMO_API_KEY'
$env:OKX_SECRET_KEY = 'YOUR_DEMO_SECRET'
$env:OKX_PASSPHRASE = 'YOUR_DEMO_PASSPHRASE'
nebula verify-demo-once
```

The command does not need `NEBULA_ENABLE_DEMO_ORDERS=true` and does not need
the emergency stop disabled. Keep the write gate disabled and the kill switch
enabled during this read-only verification.

## Interpreting the result

- `read_only_demo_connection_verified`: the API connection and the checked
  account properties passed. This is not approval to trade; the dedicated
  Demo-account attestation and all other risk gates remain separate.
- `connected_but_not_ready`: the API connected, but one or more account
  conditions need manual review. Do not enable order submission.
- `blocked`: required credentials, endpoint checks, or API reads failed.
  No order write is attempted.

If the report shows existing orders or non-USDT holdings, do not delete or
convert them just to clear the check. First identify whether they belong to
the dedicated NEBULA Demo account. Any balance adjustment or order action
requires separate explicit approval.

## Automated contract tests

CI uses a recording HTTP session with deterministic OKX-shaped responses.
It verifies the Demo header, REST host, authentication signature, documented
query parameters, Spot cash OCO payload, error handling, and zero write calls
in the verification workflow. These are offline protocol tests, not proof of
a successful connection to OKX.

## Not yet performed

No real Demo API key has been supplied or accessed by this development task.
The read-only command has not been run against the user's account. No live or
Demo order has been submitted. The 14-day forward test has not started.
