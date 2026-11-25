# Security Policy

NEBULA is an experimental, Demo-only OKX Spot trading bot. It is not approved for live trading.

## Reporting a vulnerability

Do not publish API keys, secrets, passphrases, account identifiers, private logs, or personal data in an issue or pull request. Use GitHub's private vulnerability reporting feature if it is enabled for this repository. Otherwise, contact the repository maintainer through a private channel.

## Credential and execution safety

- Never commit `.env` files or real credentials.
- Use only OKX Demo API credentials, stored locally as environment variables.
- Do not grant withdrawal permission to any API key.
- Keep `NEBULA_ENABLE_DEMO_ORDERS=false` unless explicitly conducting an authorized Demo test after all required safeguards pass.
- Do not add live-trading endpoints, live credentials, leverage, margin, or futures support to V1.
- CI runs offline unit tests and static checks only; it receives no exchange credentials and must never submit orders.

## Scope

## Regional Demo endpoint protection

- The REST hostname is selected only from the explicit allowlist in `config.py`: global, US/Australia, or EEA. The user selects the region where the OKX account was registered through `NEBULA_OKX_REGION`; no arbitrary URL can be supplied.
- All regional Demo requests must keep `x-simulated-trading: 1`, disable redirects, and use Demo API credentials. Never use a production endpoint or remove the Demo header.

## Local state protection

- NEBULA's SQLite database and its WAL/SHM/journal sidecars contain order intents, fills, position locks and audit data. They are created or hardened to owner-only file permissions; symlinked database paths are refused.
- Keep `NEBULA_DB_PATH` stable and identical for every process that is allowed to operate against one Demo account.
- The private-read rate limiter coordinates processes only on the same Linux host and under the same OS user. Separate hosts or OS users are not coordinated. Do not run concurrent instances for one OKX Demo User ID across machines.


This repository is experimental software. A passing CI run is not a security certification, profitability claim, or evidence of readiness for autonomous Demo operation.
